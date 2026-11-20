#!/usr/bin/env python3
# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""预置选品演示数据 —— 反爬兜底的安全基线。

Part 1 的 Agent 现场用 Browser 真爬 eBay 入库，但 workshop 人多并发时反爬
更严，很可能爬不动，导致数仓没数据、Part 2 分析门户演示翻车。本脚本在部署后
一次性预灌一批真实感的商品数据，让门户永远有数据可查。与爬虫完全解耦——现场
爬成功是锦上添花，爬不动也不影响演示。

落库走 Part 1 写库 Lambda（agentcore-workshop-writer）的 save_candidate /
save_verdict 两个 op，与 Agent 真实入库同一条路：UUID 主键、created_at=NOW()、
派生列（estimated_price = competitor_min_price*0.85、estimated_monthly_sales =
monthly_sales/2）、字段截断、variants JSONB 校验全在服务端做，这里只喂业务字段。

幂等：writer 每次调用服务端生成新 UUID、无 md5 去重，重跑会插重复行。所以灌入前
先经通用数据面 Lambda（agentcore-workshop-db，op=execute）查已存在的 asin_or_url
集合，按 URL 跳过已存在项——重复跑脚本不产生重复行。

时间打散：writer 把 created_at 硬编码成 NOW()，灌完数据全挤在"今天"，趋势/同环比
图不好看。灌入后再经 DB Lambda 跑一条 UPDATE，把本批数据的 created_at 打散到最近
90 天（只动最近 1 小时内落的行，避免误伤真实爬取/历史数据）。

用法（环境变量与 Part 1 部署同一套区域开关；本机或 jumphost 均可跑）：
    export AWS_PROFILE=china-cloudlab          # CloudLab 形态
    export AWS_REGION=cn-northwest-1           # 中国区；全球区用 us-west-2
    python3 scripts/seed_products.py           # 幂等灌入 + 时间打散
    python3 scripts/seed_products.py --no-spread   # 只灌入，不打散时间（不依赖 DB Lambda）
    python3 scripts/seed_products.py --wipe        # 清掉本脚本预置的数据（真实爬取数据不动）
"""

import argparse
import json
import os
import sys

import boto3
from botocore.config import Config

REGION = os.getenv("AWS_REGION", "us-west-2")
WRITER_LAMBDA = os.getenv("WRITER_LAMBDA_NAME", "agentcore-workshop-writer")
DB_LAMBDA = os.getenv("DB_LAMBDA_NAME", "agentcore-workshop-db")

# created_at 打散跨度（天）；只打散最近 1 小时内刚灌的行
SPREAD_DAYS = 90

_lambda = boto3.client(
    "lambda", region_name=REGION,
    config=Config(connect_timeout=10, read_timeout=310, retries={"max_attempts": 0}),
)


# ---- Lambda 调用封装 ----

def _invoke(fn_name: str, payload: dict) -> dict:
    resp = _lambda.invoke(FunctionName=fn_name, Payload=json.dumps(payload).encode("utf-8"))
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError") or body.get("status") != "ok":
        raise RuntimeError(f"{fn_name} {payload.get('op')} 失败: {body}")
    return body


def _writer(op: str, args: dict) -> None:
    _invoke(WRITER_LAMBDA, {"op": op, "args": args, "run_id": None})


def _db(sql: str, params: dict | None = None) -> list[dict]:
    body = _invoke(DB_LAMBDA, {"op": "execute", "args": {"sql": sql, "params": params or {}}})
    return body.get("rows") or []


# ---- 预制数据构造 helper ----

def _dom(site: str) -> str:
    return {"UK": "co.uk", "DE": "de", "US": "com"}.get(site, "co.uk")


def _rate(site: str) -> int:
    """演示固定汇率（SOP §7）：UK 英国站 9；DE 德国 / US 美国站 7。"""
    return 9 if site == "UK" else 7


def _score(comp_price: float, sales: int, site: str) -> float:
    """SOP §7 经济测算：预估产品点 = 预估售价 × 预估月销 × 汇率 / 100000。
    预估售价 = 竞对最低价 × 0.85；预估月销 = 当前月销 × 0.5。
    采集门槛是 > 0.0005；候选都已过门槛，故恒 > 0.0005。"""
    return round(comp_price * 0.85 * (sales * 0.5) * _rate(site) / 100000, 4)


def _cand(idx, site, name_cn, title_en, keyword, price, sales, comp_price,
          flag, variants, cert, material, summary):
    """构造一条候选档案（verdict=采集 的完整商品）。idx 决定稳定的 URL / 图片。
    product_score 不手填——按 SOP §7 公式实算（见 _score），与 Agent 真实入库
    同一口径（门户把该列当『产品点』以 4 位小数展示并排 top-N，手填 5 分制评分
    会穿帮）。"""
    itm = 2000000000 + idx
    return {
        "site": site,
        "asin_or_url": f"https://www.ebay.{_dom(site)}/itm/{itm}",
        "product_name_cn": name_cn,
        "product_title_en": title_en,
        "search_keyword": keyword,
        "current_price": price,
        "monthly_sales": sales,
        "competitor_min_price": comp_price,
        "competitor_min_price_url": f"https://www.ebay.{_dom(site)}/itm/{itm + 700000}",
        "product_score": _score(comp_price, sales, site),
        "price_flag": flag,
        "main_image_url": f"https://i.ebayimg.com/images/g/seedc{idx:03d}/s-l1600.jpg",
        "variants": json.dumps(variants, ensure_ascii=False),
        "certification": cert,
        "material_spec": material,
        "ai_summary": summary,
    }


def _vd(idx, site, title, verdict, reason, price, sales, score):
    """构造一条纯判定流水（不进候选池）。idx 偏移到独立号段避免撞候选 URL。"""
    itm = 2100000000 + idx
    return {
        "site": site,
        "asin_or_url": f"https://www.ebay.{_dom(site)}/itm/{itm}",
        "product_title": title,
        "verdict": verdict,
        "reason": reason,
        "current_price": price,
        "monthly_sales": sales,
        "product_score": score,
    }


# ---- 候选档案（62 条，全部判定=采集；9 个赛道 × UK/DE/US 三站聚类） ----
# 含真实提示词主线赛道 desk organizer（桌面收纳）；每条候选同时在判定表落一条「采集」。
# 价格带 £7~$45、月销 180~680（候选都过了 SOP R1：价 > £5/$5 且月销 > 1）。
# product_score 按 SOP §7 公式实算（_score），数量级 0.0x~0.x，恒 > 0.0005 门槛。

CANDIDATES = [
    # 厨房小工具
    _cand(1, "UK", "多功能蔬菜螺旋切丝器", "5-Blade Vegetable Spiralizer Slicer",
          "kitchen gadget", 12.99, 420, 9.50, "正常",
          {"color": ["white", "green"], "blades": 5},
          "CE, LFGB", "ABS 外壳 + 304 不锈钢刀片",
          "低客单高复购的厨房爆款，5 刀片差异化明显，竞品均价偏高，毛利空间可观。"),
    _cand(2, "UK", "手持电动奶泡打发器", "Handheld Electric Milk Frother Whisk",
          "kitchen gadget", 8.49, 610, 6.20, "正常",
          {"color": ["black", "silver", "rose gold"]},
          "CE, RoHS", "不锈钢搅拌头 + 食品级塑料",
          "咖啡周边刚需小件，三色可选，月销稳定，适合做组合引流。"),
    _cand(3, "DE", "硅胶可折叠漏斗套装", "Collapsible Silicone Funnel Set of 3",
          "kitchen gadget", 7.99, 230, 5.80, "正常",
          {"size": ["S", "M", "L"], "pack": 3},
          "LFGB", "食品级硅胶",
          "德国站家居收纳向，三件套客单略低但复购高，竞品同质化，靠评分取胜。"),
    _cand(4, "UK", "不锈钢多功能开瓶器", "Stainless Steel 4-in-1 Bottle Opener",
          "kitchen gadget", 9.99, 340, 7.10, "正常",
          {"color": ["silver", "black"]},
          "CE", "304 不锈钢",
          "酒类配件常青款，四合一卖点清晰，售价与竞品接近靠评分和图差异化。"),
    _cand(5, "DE", "按压式计量咖啡勺", "Press-Measure Coffee Scoop with Clip",
          "kitchen gadget", 10.50, 180, 8.00, "正常",
          {"capacity": ["10g", "15g"]},
          "LFGB", "PP + 不锈钢",
          "咖啡计量细分需求，月销中等但利润率不错，可作咖啡赛道补充 SKU。"),

    # 宠物用品
    _cand(6, "UK", "宠物自动逗猫激光器", "Automatic Rotating Cat Laser Toy",
          "pet supplies", 15.99, 520, 11.90, "正常",
          {"mode": ["auto", "manual"], "power": ["USB", "battery"]},
          "CE, RoHS", "ABS 外壳",
          "宠物电动玩具高增长赛道，自动模式解放双手卖点强，月销高、评分优。"),
    _cand(7, "UK", "可水洗宠物除毛手套", "Washable Pet Grooming Glove Pair",
          "pet supplies", 7.49, 680, 5.00, "正常",
          {"hand": ["left", "right", "pair"], "color": ["blue", "pink"]},
          "CE", "硅胶 + 网布",
          "低客单高周转的宠物清洁刚需，月销 680 领跑，适合主推走量。"),
    _cand(8, "DE", "折叠硅胶宠物饮水碗", "Collapsible Silicone Pet Travel Bowl",
          "pet supplies", 8.99, 410, 6.40, "正常",
          {"size": ["350ml", "500ml"], "color": ["green", "orange"]},
          "LFGB", "食品级硅胶",
          "出行场景宠物碗，德国站户外人群大，两规格两色覆盖主流需求。"),
    _cand(9, "UK", "宠物指甲剪带 LED 灯", "Pet Nail Clipper with LED Light",
          "pet supplies", 11.49, 290, 8.80, "正常",
          {"size": ["small", "large"]},
          "CE", "不锈钢刀头 + ABS",
          "宠物美容细分，LED 防剪血线卖点差异化，月销中等但转化稳。"),
    _cand(10, "UK", "狗狗漏食慢食益智碗", "Slow Feeder Dog Bowl Anti-Gulping",
           "pet supplies", 13.99, 360, 10.20, "正常",
           {"color": ["grey", "teal", "pink"], "size": ["M", "L"]},
           "LFGB", "食品级 PP",
           "健康喂养趋势品，防暴食卖点契合养宠痛点，三色两规格 SKU 组合好。"),

    # 园艺户外
    _cand(11, "UK", "便携式手持除草松土器", "Handheld Garden Weeder Cultivator",
           "garden tool", 14.50, 240, 10.90, "正常",
           {"length": ["30cm", "40cm"]},
           "CE", "碳钢 + 榉木手柄",
           "园艺工具季节性强但复购稳，木柄质感拉高溢价，评分扎实。"),
    _cand(12, "DE", "太阳能花园装饰地插灯 4 件", "Solar Garden Stake Lights 4-Pack",
           "garden tool", 18.99, 450, 13.50, "正常",
           {"color": ["warm white", "RGB"], "pack": 4},
           "CE, RoHS, IP65", "ABS + 单晶硅板",
           "德国站园艺照明旺季爆款，IP65 防水 + RGB 卖点足，四件套客单高。"),
    _cand(13, "UK", "可伸缩花园浇水软管", "Expandable Garden Hose 15m",
           "garden tool", 22.99, 310, 17.80, "正常",
           {"length": ["7.5m", "15m", "30m"]},
           "CE", "TPC 内管 + 乳胶",
           "园艺大件客单高、利润绝对值大，三长度覆盖庭院场景，评分口碑好。"),
    _cand(14, "DE", "多功能园艺手套防刺", "Thorn-Proof Gardening Gloves",
           "garden tool", 9.90, 280, 7.00, "正常",
           {"size": ["M", "L", "XL"]},
           "CE, EN388", "合成革 + 涤纶",
           "园艺防护刚需，EN388 认证背书，三码齐全，月销中等稳定走量。"),
    _cand(15, "UK", "户外防水植物保温罩", "Outdoor Plant Frost Protection Cover",
           "garden tool", 11.99, 160, 8.50, "正常",
           {"size": ["0.8x1m", "1.5x2m"]},
           "CE", "无纺布 PP",
           "冬季护苗季节品，月销偏低但淡旺季明显，可作旺季前铺货测款。"),

    # 家居收纳
    _cand(16, "UK", "门后挂式多层收纳袋", "Over-Door Hanging Storage Organizer",
           "home storage", 13.50, 390, 9.90, "正常",
           {"pockets": ["12", "24"], "color": ["grey", "beige"]},
           "CE", "无纺布 + PVC",
           "小户型收纳刚需，门后免打孔卖点强，两规格两色 SKU 组合覆盖广。"),
    _cand(17, "DE", "真空压缩收纳袋 6 件套", "Vacuum Compression Storage Bags 6-Pack",
           "home storage", 16.99, 470, 12.60, "正常",
           {"size": ["mixed", "L", "XL"], "pack": 6},
           "CE", "PA + PE 复合膜",
           "换季收纳高频需求，德国站居家囤货多，六件套客单高、复购强。"),
    _cand(18, "UK", "可堆叠抽屉分隔收纳盒", "Stackable Drawer Divider Organizer",
           "home storage", 10.99, 350, 8.00, "正常",
           {"color": ["clear", "white"], "pack": ["4", "8"]},
           "LFGB", "PP",
           "抽屉整理常青品，可堆叠模块化卖点好，评分稳、复购高。"),
    _cand(19, "DE", "床底滚轮扁平收纳箱", "Under-Bed Rolling Storage Box",
           "home storage", 19.90, 220, 15.00, "正常",
           {"size": ["60L", "80L"], "color": ["grey"]},
           "CE", "PP + 金属滚轮",
           "床底空间利用大件，客单高利润绝对值可观，月销中等但稳。"),
    _cand(20, "UK", "免打孔浴室置物架", "No-Drill Bathroom Shelf Organizer",
           "home storage", 14.99, 300, 11.00, "正常",
           {"tier": ["2", "3"], "color": ["white", "black"]},
           "CE", "铝合金 + ABS",
           "浴室免打孔收纳契合租房人群，两层/三层两色 SKU 组合覆盖主流。"),

    # 车载配件
    _cand(21, "UK", "重力感应车载手机支架", "Gravity Auto-Clamp Car Phone Mount",
           "car accessory", 12.99, 540, 9.20, "正常",
           {"mount": ["vent", "dashboard"], "color": ["black"]},
           "CE, RoHS", "ABS + 硅胶",
           "车载刚需高频品，重力自动夹卖点成熟，月销高、评分优，适合主推。"),
    _cand(22, "DE", "车载无线充电出风口支架", "Wireless Charging Car Mount 15W",
           "car accessory", 24.99, 330, 18.90, "正常",
           {"power": ["10W", "15W"]},
           "CE, RoHS, Qi", "ABS + 铝",
           "带无线充的升级款，客单显著更高，Qi 认证背书，德国站接受度好。"),
    _cand(23, "UK", "多功能车载座椅缝隙收纳盒", "Car Seat Gap Filler Organizer",
           "car accessory", 10.49, 410, 7.50, "正常",
           {"side": ["driver", "passenger", "pair"]},
           "CE", "PU 皮 + ABS",
           "车内收纳细分刚需，缝隙防掉落痛点清晰，成对卖提升客单。"),
    _cand(24, "DE", "车载后备箱折叠收纳箱", "Collapsible Car Trunk Organizer",
           "car accessory", 17.99, 260, 13.20, "正常",
           {"size": ["single", "double"], "color": ["black", "grey"]},
           "CE", "牛津布 + 硬质底板",
           "后备箱整理大件，德国站旅行自驾多，可折叠卖点好、利润绝对值高。"),
    _cand(25, "UK", "汽车内饰缝隙清洁刷套装", "Car Detailing Cleaning Brush Set",
           "car accessory", 9.99, 370, 7.00, "正常",
           {"pack": ["5", "9"]},
           "CE", "尼龙刷毛 + PP",
           "汽车美容耗材复购高，五件/九件两套覆盖入门到进阶，走量稳。"),

    # 美妆个护工具
    _cand(26, "UK", "静音双头睫毛电烫器", "Rechargeable Heated Eyelash Curler",
           "beauty tool", 13.99, 480, 10.10, "正常",
           {"color": ["pink", "white", "black"]},
           "CE, RoHS", "ABS + 硅胶加热片",
           "美妆小电器高增长，USB 充电 + 双头卖点足，三色月销高、评分优。"),
    _cand(27, "DE", "可水洗电动洁面仪", "Waterproof Silicone Facial Cleansing Brush",
           "beauty tool", 16.99, 350, 12.40, "正常",
           {"color": ["mint", "pink", "grey"], "mode": ["3-speed"]},
           "CE, RoHS, IPX7", "医用级硅胶",
           "个护洁面仪刚需，IPX7 全身防水背书，德国站接受度高、复购好。"),
    _cand(28, "UK", "负离子迷你便携卷发棒", "Mini Portable Ionic Hair Curler",
           "beauty tool", 19.99, 290, 15.00, "正常",
           {"barrel": ["25mm", "32mm"], "voltage": ["dual"]},
           "CE, RoHS", "陶瓷涂层 + PC",
           "旅行美发电器，双电压适合出行人群，客单较高、评分稳。"),
    _cand(29, "DE", "多功能电动修眉刀", "Electric Eyebrow Trimmer Set",
           "beauty tool", 11.49, 320, 8.60, "正常",
           {"heads": ["3", "5"]},
           "CE, RoHS", "ABS + 不锈钢刀头",
           "个护修容细分，多刀头一机多用卖点好，月销中等、复购稳。"),
    _cand(30, "UK", "LED 化妆镜折叠便携款", "Folding LED Travel Makeup Mirror",
           "beauty tool", 14.50, 260, 10.80, "正常",
           {"light": ["warm", "cool", "dual"], "fold": ["trifold"]},
           "CE, RoHS", "ABS + 高清镜面",
           "美妆周边便携镜，三色温卖点差异化，旅行场景客单可观。"),

    # 婴童用品
    _cand(31, "UK", "婴儿硅胶防摔餐盘吸盘款", "Suction Silicone Baby Feeding Plate",
           "baby product", 12.99, 430, 9.50, "正常",
           {"color": ["green", "grey", "pink"], "shape": ["bear", "round"]},
           "LFGB, EN14372", "食品级硅胶",
           "婴童喂养高频刚需，吸盘防摔痛点强，EN14372 认证，月销高、评分优。"),
    _cand(32, "DE", "多功能婴儿车挂钩 2 件", "Stroller Hooks Clip 2-Pack",
           "baby product", 7.99, 560, 5.40, "正常",
           {"color": ["black", "grey"], "pack": 2},
           "CE", "锌合金 + 硅胶",
           "婴童出行低客单爆款，承重卖点清晰，德国站月销高、走量快。"),
    _cand(33, "UK", "婴儿恒温洗澡水温计", "Baby Bath Thermometer Float",
           "baby product", 9.49, 310, 7.00, "正常",
           {"shape": ["duck", "whale"]},
           "CE, EN71", "ABS + 食品级材料",
           "婴童洗护安全品，EN71 玩具安全认证背书，造型差异化、评分稳。"),
    _cand(34, "DE", "便携式婴儿尿布收纳包", "Portable Baby Diaper Caddy Organizer",
           "baby product", 15.99, 240, 11.90, "正常",
           {"color": ["grey", "beige"], "size": ["M", "L"]},
           "CE", "毛毡 + 棉",
           "母婴收纳中客单品，德国站囤货多，两色两规格 SKU 组合覆盖主流。"),
    _cand(35, "UK", "婴儿安抚牙胶冰凉款", "Soothing Water-Filled Baby Teether",
           "baby product", 8.49, 390, 6.10, "正常",
           {"color": ["blue", "pink", "green"], "pack": ["2", "4"]},
           "LFGB, EN71", "食品级 TPE",
           "出牙期刚需耗材，可冰镇卖点契合痛点，多色多规格、复购高。"),

    # 健身小件
    _cand(36, "UK", "可调节阻力健身弹力带套装", "Resistance Bands Set 5 Levels",
           "fitness gear", 13.99, 500, 10.00, "正常",
           {"levels": 5, "color": ["mixed"]},
           "CE, RoHS", "天然乳胶",
           "家庭健身高增长赛道，五阻力分级卖点足，月销高、评分优、适合主推。"),
    _cand(37, "DE", "硅胶计数跳绳电子款", "Digital Counting Jump Rope",
           "fitness gear", 11.49, 360, 8.40, "正常",
           {"mode": ["corded", "cordless"], "color": ["black", "blue"]},
           "CE, RoHS", "PVC + ABS",
           "健身小件带电子计数升级卖点，有/无绳两模式覆盖室内外，评分稳。"),
    _cand(38, "UK", "泡沫轴肌肉放松滚轮", "EVA Foam Roller Muscle Massage",
           "fitness gear", 16.99, 280, 12.80, "正常",
           {"length": ["33cm", "45cm"], "color": ["black", "purple"]},
           "CE", "EVA + PP 芯",
           "运动恢复中客单品，两长度两色覆盖进阶人群，利润绝对值可观。"),
    _cand(39, "DE", "便携腕式握力器可调节", "Adjustable Hand Grip Strengthener",
           "fitness gear", 9.99, 420, 7.20, "正常",
           {"resistance": ["10-40kg"], "color": ["black", "red"]},
           "CE, RoHS", "ABS + 金属弹簧",
           "握力训练低客单高周转，可调阻力一器多用，德国站月销高、走量快。"),
    _cand(40, "UK", "瑜伽辅助拉伸带分段款", "Yoga Stretching Strap with Loops",
           "fitness gear", 8.99, 330, 6.50, "正常",
           {"loops": ["8", "10"], "color": ["grey", "teal"]},
           "CE", "涤纶 + 棉",
           "瑜伽辅具低客单刚需，分段环卖点清晰，月销稳定、复购好。"),

    # ===== 美国站（ebay.com，美元价位，FCC/UL/FDA/ASTM 美标认证）=====
    # 厨房小工具
    _cand(41, "US", "电动手持搅拌打蛋器", "Electric Handheld Immersion Blender",
          "kitchen gadget", 18.99, 580, 14.20, "正常",
          {"color": ["black", "silver"], "speed": ["2-speed"]},
          "FDA, UL", "304 不锈钢 + 食品级塑料",
          "美国站厨房小电器高增长，UL 认证背书，月销高、评分优，适合主推走量。"),
    _cand(42, "US", "可拆洗蒜泥研磨器", "Rotary Garlic Press Mincer Washable",
          "kitchen gadget", 11.49, 390, 8.50, "正常",
          {"color": ["red", "green", "white"]},
          "FDA", "ABS + 食品级 PP",
          "美国站厨房刚需小件，可拆洗卖点好，三色月销稳定，复购高。"),
    # 宠物用品
    _cand(43, "US", "智能宠物自动喂食器", "Automatic Pet Feeder with Timer",
          "pet supplies", 34.99, 420, 26.00, "正常",
          {"capacity": ["3L", "5L"], "power": ["USB", "battery"]},
          "FCC, UL", "ABS 外壳 + 食品级料仓",
          "美国站宠物智能品高客单，定时喂食契合上班族痛点，评分优、利润绝对值高。"),
    _cand(44, "US", "宠物自动饮水循环喷泉", "Pet Water Fountain Dispenser 2.5L",
          "pet supplies", 25.99, 360, 19.50, "正常",
          {"capacity": ["2.5L"], "color": ["white", "grey"]},
          "FCC, UL", "食品级 PP + 活性炭滤芯",
          "美国站宠物健康赛道，循环活水卖点强，带滤芯复购耗材，月销稳。"),
    # 园艺户外
    _cand(45, "US", "重型花园伸缩修枝剪", "Heavy-Duty Telescopic Pruning Shears",
          "garden tool", 29.99, 260, 22.80, "正常",
          {"length": ["65cm", "90cm"]},
          "ANSI", "SK5 碳钢刀头 + 铝合金杆",
          "美国站园艺重型工具客单高，伸缩够高处卖点好，利润绝对值可观。"),
    _cand(46, "US", "太阳能驱虫庭院灯", "Solar Bug Zapper Garden Light",
          "garden tool", 21.99, 410, 16.40, "正常",
          {"color": ["black"], "pack": ["1", "2"]},
          "FCC, IP65", "ABS + 单晶硅板",
          "美国站庭院照明+驱虫二合一，IP65 防水，旺季月销高，两件装提客单。"),
    # 家居收纳
    _cand(47, "US", "可叠放衣柜分层收纳架", "Stackable Closet Shelf Organizer",
          "home storage", 23.99, 340, 18.00, "正常",
          {"tier": ["2", "3"], "color": ["white", "grey"]},
          "ASTM", "钢管 + 无纺布",
          "美国站衣柜收纳中客单，可叠放模块化卖点好，两规格两色覆盖主流需求。"),
    _cand(48, "US", "大容量真空密封储物罐 4 件", "Airtight Vacuum Food Container 4-Set",
          "home storage", 27.99, 290, 21.00, "正常",
          {"size": ["mixed"], "pack": 4},
          "FDA, BPA-Free", "Tritan + 硅胶密封圈",
          "美国站厨房储物高频需求，BPA-Free 契合健康诉求，四件套客单高、复购强。"),
    # 车载配件
    _cand(49, "US", "车载双 USB 快充点烟器", "Dual USB Car Charger Fast Charge 36W",
          "car accessory", 15.99, 620, 11.50, "正常",
          {"power": ["36W"], "color": ["black"]},
          "FCC, UL", "阻燃 PC + 铝合金",
          "美国站车载充电刚需爆款，36W 快充 + UL 认证，月销 620 领跑，适合主推。"),
    _cand(50, "US", "车载磁吸仪表台支架", "Magnetic Dashboard Phone Mount",
          "car accessory", 13.49, 450, 9.80, "正常",
          {"color": ["black"], "mount": ["dashboard", "vent"]},
          "FCC", "锌合金 + 强磁 + 硅胶",
          "美国站车载支架高频品，磁吸单手操作卖点成熟，月销高、评分稳。"),
    # 美妆个护工具
    _cand(51, "US", "USB 充电补水喷雾仪", "Rechargeable Facial Nano Mister",
          "beauty tool", 22.99, 380, 17.20, "正常",
          {"color": ["white", "pink", "black"], "capacity": ["30ml"]},
          "FCC, FDA", "ABS + 食品级水箱",
          "美国站个护美容小电器，便携补水卖点契合通勤人群，三色月销高、复购好。"),
    _cand(52, "US", "无线 LED 美甲固化灯", "Cordless LED UV Nail Lamp 48W",
          "beauty tool", 29.99, 300, 23.00, "正常",
          {"power": ["48W"], "color": ["white", "pink"]},
          "FCC, RoHS", "ABS + LED 灯珠",
          "美国站美甲工具中高客单，无线便携 + 48W 速干卖点足，评分稳、利润好。"),
    # 婴童用品
    _cand(53, "US", "婴儿硅胶学饮吸管杯", "Spill-Proof Silicone Baby Straw Cup",
          "baby product", 14.99, 490, 11.00, "正常",
          {"color": ["mint", "grey", "pink"], "capacity": ["250ml"]},
          "FDA, CPSIA, BPA-Free", "食品级硅胶",
          "美国站婴童喂养刚需，防漏学饮 + CPSIA 认证，三色月销高、评分优、复购强。"),
    _cand(54, "US", "便携式婴儿安抚夜灯", "Portable Baby Night Light Rechargeable",
          "baby product", 17.99, 320, 13.40, "正常",
          {"color": ["warm white", "RGB"], "mode": ["touch", "remote"]},
          "FCC, ASTM F963", "ABS + 食品级硅胶灯罩",
          "美国站婴童夜间安抚品，可充电 + 遥控卖点好，ASTM 玩具安全认证背书。"),
    # 健身小件
    _cand(55, "US", "可调节哑铃单只 25lb", "Adjustable Dumbbell Single 25lb",
          "fitness gear", 44.99, 350, 36.00, "正常",
          {"weight": ["15lb", "25lb"], "color": ["black"]},
          "ASTM", "铸铁 + 防滑橡胶包层",
          "美国站家庭健身大件高客单，可调重量一器多用卖点强，月销高、利润绝对值大。"),
    _cand(56, "US", "硅胶按摩筋膜球套装", "Massage Lacrosse Ball Set of 3",
          "fitness gear", 13.99, 410, 10.20, "正常",
          {"firmness": ["soft", "medium", "firm"], "pack": 3},
          "ASTM", "TPE + EVA",
          "美国站运动恢复低客单高周转，三硬度套装覆盖进阶，月销稳、复购高。"),

    # ===== desk organizer 桌面收纳（真实提示词的主线赛道，关键词 "desk organizer"）=====
    # 纯家居非食品非电子，认证按 SOP §10 普通家居品处理；UK/DE 汇率 9/7、US 7。
    _cand(57, "UK", "多层桌面文具收纳架", "Mesh Desk Organizer 5-Compartment",
          "desk organizer", 15.99, 460, 11.80, "正常",
          {"color": ["black", "white", "rose gold"], "tier": ["3", "5"]},
          "无强制认证（普通家居品，符合 UK REACH 即可）", "金属网 + 防滑脚垫",
          "桌面收纳刚需高频赛道，多格差异化清晰，月销高、竞品均价偏高毛利可观，产品点达标采集。"),
    _cand(58, "UK", "竹制可扩展抽屉分隔收纳盒", "Bamboo Expandable Desk Drawer Organizer",
          "desk organizer", 18.99, 300, 14.20, "正常",
          {"size": ["S", "L"], "slots": ["5", "7"]},
          "无强制认证（普通家居木制品，符合 UK REACH 即可）", "天然竹 + 环保漆",
          "办公收纳升级款，竹制质感拉高溢价，可扩展卖点契合不同抽屉，评分口碑好。"),
    _cand(59, "DE", "旋转式桌面笔筒收纳盒", "360° Rotating Desk Pen Holder Organizer",
          "desk organizer", 16.49, 380, 12.00, "正常",
          {"color": ["grey", "white"], "layers": ["2", "3"]},
          "无强制认证（普通家居品，符合 EU REACH 即可）", "ABS + 防滑底座",
          "德国站办公收纳大，旋转取用卖点好，两色两层 SKU 组合覆盖主流，月销稳、产品点达标。"),
    _cand(60, "DE", "亚克力透明化妆桌面收纳盒", "Clear Acrylic Desk Cosmetic Organizer",
          "desk organizer", 21.99, 260, 16.50, "正常",
          {"drawers": ["2", "4"], "size": ["M", "L"]},
          "无强制认证（普通家居品，符合 EU REACH 即可）", "亚克力 + 植绒内衬",
          "桌面+美妆收纳跨场景中客单，透明高级感卖点足，德国站接受度高、利润绝对值可观。"),
    _cand(61, "US", "金属网桌面文件分层架", "Metal Mesh Desk File Sorter Organizer",
          "desk organizer", 22.99, 420, 17.30, "正常",
          {"color": ["black", "silver"], "slots": ["3", "5"]},
          "无强制认证（普通家居办公品）", "金属网 + 烤漆",
          "美国站办公收纳高频刚需，网格分层卖点清晰，月销高、竞品均价高毛利可观，产品点达标采集。"),
    _cand(62, "US", "可叠放办公桌抽屉整理盒套装", "Stackable Desk Drawer Organizer Set 6pc",
          "desk organizer", 19.99, 350, 15.00, "正常",
          {"pack": ["4", "6"], "color": ["clear", "white"]},
          "无强制认证（普通家居品，BPA-Free）", "食品级 PP",
          "美国站桌面整理套装中客单，可叠放模块化卖点好，六件套覆盖多场景、复购强、评分稳。"),
]


# ---- 纯判定流水（不进候选池；给漏斗分布，含 UK/DE/US 三站）----
# verdict 严格按 product-selection-ebay SOP 决策树归类（§4/§5/§6/§7/§11）：
#   PASS   —— R1 硬门槛不过：价 ≤ £5/$5 或 月销 ≤ 1（在 R1 就停，不走经济测算）。
#   排除   —— 仅四类依据：上架 ≥ 3 年(估)、品牌/卖家垄断、6 项黑名单
#             （大牌垄断/可定制/消耗品/3C 电子/服装箱包/品牌官方直营）、产品点 ≤ 0.0005。
#   补采   —— 数据缺失/抓取失败（竞品价/月销/认证/变体未抓到、反爬拦截）。
# product_score 口径：PASS 在 R1 停、黑名单/垄断排除没走到经济测算 → 0.0；
#   仅「产品点 ≤ 0.0005」这类排除填算出的小值（见 _score 公式，恒 ≤ 0.0005）。
# 分布：PASS 30 / 排除 37 / 补采 19（低价、单件月销≤1、黑名单、垄断、上架过老、产品点过低齐全）。
# 合并候选表的 62 条「采集」后，漏斗总判定 148 条：采集 62 / PASS 30 / 排除 37 / 补采 19。

EXTRA_VERDICTS = [
    # ========== PASS：R1 硬门槛不过（价 ≤ £5/$5 或 月销 ≤ 1）==========
    # ---- 价 ≤ £5（UK/DE，低价不过 R1，SOP §11: if 当前价<=5 return PASS）----
    _vd(1, "UK", "Mini Silicone Ice Cube Tray", "PASS", "价 £4.49 ≤ £5，R1 硬门槛不过", 4.49, 210, 0.0),
    _vd(2, "UK", "Reusable Coffee Filter Mesh", "PASS", "价 £3.99 ≤ £5，R1 硬门槛不过", 3.99, 260, 0.0),
    _vd(3, "DE", "Plastic Teaspoon Set 12pc", "PASS", "价 €2.49 ≤ €5，R1 硬门槛不过", 2.49, 180, 0.0),
    _vd(4, "UK", "Paper Straws Disposable 100pc", "PASS", "价 £3.29 ≤ £5，R1 硬门槛不过", 3.29, 160, 0.0),
    _vd(5, "DE", "Rubber Band Assorted Bag", "PASS", "价 €1.99 ≤ €5，R1 硬门槛不过", 1.99, 140, 0.0),
    _vd(6, "UK", "Hair Tie Elastic 100pcs", "PASS", "价 £2.59 ≤ £5，R1 硬门槛不过", 2.59, 170, 0.0),
    _vd(7, "DE", "Mini Notebook A7 Plain", "PASS", "价 €2.79 ≤ €5，R1 硬门槛不过", 2.79, 130, 0.0),
    _vd(8, "UK", "Plastic Comb Basic 5pc", "PASS", "价 £1.89 ≤ £5，R1 硬门槛不过", 1.89, 100, 0.0),
    _vd(9, "DE", "Keychain Ring 20pcs", "PASS", "价 €2.99 ≤ €5，R1 硬门槛不过", 2.99, 120, 0.0),
    _vd(10, "UK", "Balloon Pack Mixed 50", "PASS", "价 £2.99 ≤ £5，R1 硬门槛不过", 2.99, 95, 0.0),
    _vd(11, "DE", "Cable Ties 100pc Black", "PASS", "价 €3.49 ≤ €5，R1 硬门槛不过", 3.49, 150, 0.0),
    _vd(12, "UK", "Clothes Pegs Plastic 48pc", "PASS", "价 £3.79 ≤ £5，R1 硬门槛不过", 3.79, 190, 0.0),
    _vd(13, "DE", "Sticky Notes 400 Sheets", "PASS", "价 €4.29 ≤ €5，R1 硬门槛不过", 4.29, 140, 0.0),
    _vd(14, "UK", "Birthday Candles 24pc", "PASS", "价 £2.19 ≤ £5，R1 硬门槛不过", 2.19, 110, 0.0),
    _vd(15, "DE", "Wooden Lolly Sticks 150pc", "PASS", "价 €3.99 ≤ €5，R1 硬门槛不过", 3.99, 125, 0.0),
    # ---- 月销 ≤ 1（估算，SOP §11: if 总月销<=1 return PASS，注明估算口径）----
    _vd(16, "UK", "Niche Vinyl Record Cleaner", "PASS", "月销 1（累计÷12 保守估算）≤ 1，R1 硬门槛不过", 13.99, 1, 0.0),
    _vd(17, "DE", "Antique Typewriter Ribbon", "PASS", "月销 0（无购买记录估算）≤ 1，R1 硬门槛不过", 9.99, 0, 0.0),
    _vd(18, "UK", "Left-Handed Can Opener", "PASS", "月销 1（累计÷12 保守估算）≤ 1，R1 硬门槛不过", 10.49, 1, 0.0),
    _vd(19, "DE", "Specialty Fishing Lure Rare", "PASS", "月销 1（累计÷在售月数估算）≤ 1，R1 硬门槛不过", 14.99, 1, 0.0),
    _vd(20, "UK", "Vintage Camera Strap Leather", "PASS", "月销 1（累计÷12 保守估算）≤ 1，R1 硬门槛不过", 16.99, 1, 0.0),
    _vd(21, "DE", "Obscure Watch Repair Kit", "PASS", "月销 0（无购买记录估算）≤ 1，R1 硬门槛不过", 12.49, 0, 0.0),
    _vd(22, "UK", "Rare Stamp Collecting Tongs", "PASS", "月销 1（累计÷12 保守估算）≤ 1，R1 硬门槛不过", 8.99, 1, 0.0),
    _vd(23, "DE", "Niche Model Train Oil", "PASS", "月销 1（累计÷在售月数估算）≤ 1，R1 硬门槛不过", 7.49, 1, 0.0),
    # ---- 美国站 PASS（价 ≤ $5 或 月销 ≤ 1）----
    _vd(24, "US", "Plastic Spoons Disposable 100ct", "PASS", "价 $3.49 ≤ $5，R1 硬门槛不过", 3.49, 190, 0.0),
    _vd(25, "US", "Paper Napkins Bulk 500ct", "PASS", "价 $4.29 ≤ $5，R1 硬门槛不过", 4.29, 160, 0.0),
    _vd(26, "US", "Twist Ties 200pc Green", "PASS", "价 $2.99 ≤ $5，R1 硬门槛不过", 2.99, 150, 0.0),
    _vd(27, "US", "Index Cards 300ct Ruled", "PASS", "价 $4.49 ≤ $5，R1 硬门槛不过", 4.49, 130, 0.0),
    _vd(28, "US", "Niche Coin Collector Sleeves", "PASS", "月销 1（累计÷12 保守估算）≤ 1，R1 硬门槛不过", 11.99, 1, 0.0),
    _vd(29, "US", "Rare Fountain Pen Converter", "PASS", "月销 0（无购买记录估算）≤ 1，R1 硬门槛不过", 9.99, 0, 0.0),
    _vd(30, "US", "Vintage Radio Tube NOS", "PASS", "月销 1（累计÷在售月数估算）≤ 1，R1 硬门槛不过", 14.49, 1, 0.0),

    # ========== 排除：仅 SOP 四类依据（§5 市场结构 / §6 黑名单 / §7 产品点）==========
    # ---- 黑名单 3「消耗品」（一次性/快速耗尽需复购：食品/耗材/电池本体）----
    _vd(31, "UK", "Coffee Capsules Compatible 100pc", "排除", "黑名单：消耗品（咖啡耗材需高频复购）", 18.99, 220, 0.0),
    _vd(32, "DE", "Dishwasher Tablets 60pc", "排除", "黑名单：消耗品（清洁耗材需复购）", 14.99, 260, 0.0),
    _vd(33, "UK", "Protein Bars Box 24", "排除", "黑名单：消耗品（食品）", 22.99, 180, 0.0),
    _vd(34, "DE", "AA Alkaline Batteries 48pc", "排除", "黑名单：消耗品（电池本体）", 16.49, 300, 0.0),
    _vd(35, "UK", "Vacuum Cleaner Bags 20pc", "排除", "黑名单：消耗品（耗材需持续复购）", 12.99, 210, 0.0),
    _vd(36, "DE", "Air Freshener Refill 6pc", "排除", "黑名单：消耗品（香氛耗材）", 9.99, 240, 0.0),
    _vd(37, "US", "Dryer Sheets 240ct", "排除", "黑名单：消耗品（洗护耗材需复购）", 13.99, 230, 0.0),
    _vd(38, "US", "Water Filter Cartridge 6pc", "排除", "黑名单：消耗品（滤芯耗材）", 29.99, 190, 0.0),
    # ---- 黑名单 4「3C / 电子」（合规、认证、技术门槛高）----
    _vd(39, "UK", "Wireless Earbuds Bluetooth 5.3", "排除", "黑名单：3C 电子（认证/技术门槛高）", 24.99, 340, 0.0),
    _vd(40, "DE", "Power Bank 20000mAh", "排除", "黑名单：3C 电子（含电池，合规门槛高）", 27.99, 280, 0.0),
    _vd(41, "UK", "USB-C Hub 7-in-1", "排除", "黑名单：3C 电子（数码配件）", 32.99, 220, 0.0),
    _vd(42, "DE", "Smart LED Bulb WiFi", "排除", "黑名单：3C 电子（智能电子）", 15.99, 310, 0.0),
    _vd(43, "US", "Mini Projector 1080P", "排除", "黑名单：3C 电子（数码整机，技术门槛高）", 44.99, 180, 0.0),
    _vd(44, "US", "Bluetooth Speaker Waterproof", "排除", "黑名单：3C 电子（含电池/认证）", 29.99, 350, 0.0),
    # ---- 黑名单 5「服装 / 箱包」（SKU 爆炸，退货率高）----
    _vd(45, "UK", "Men's Cotton T-Shirt Multipack", "排除", "黑名单：服装（SKU 爆炸退货高）", 19.99, 260, 0.0),
    _vd(46, "DE", "Canvas Backpack 25L", "排除", "黑名单：箱包（SKU 多退货高）", 29.99, 210, 0.0),
    _vd(47, "UK", "Women's Knit Scarf Winter", "排除", "黑名单：服装（SKU 爆炸退货高）", 12.99, 230, 0.0),
    _vd(48, "US", "Running Shoes Mesh Unisex", "排除", "黑名单：鞋帽（尺码 SKU 爆炸退货高）", 34.99, 290, 0.0),
    # ---- 黑名单 2「可定制」（刻字/印名/尺寸定做，Personalised/Custom）----
    _vd(49, "UK", "Personalised Engraved Keyring", "排除", "黑名单：可定制（Personalised 刻字定做）", 9.99, 320, 0.0),
    _vd(50, "DE", "Custom Name Door Sign Wood", "排除", "黑名单：可定制（按买家姓名定做）", 14.99, 180, 0.0),
    _vd(51, "US", "Custom Photo Phone Case", "排除", "黑名单：可定制（买家上传图定制）", 11.99, 410, 0.0),
    # ---- 黑名单 1 / §5「大品牌垄断」（品牌第一名份额 > 60%）----
    _vd(52, "UK", "Branded Vacuum Spare Part", "排除", "大品牌垄断：头部品牌可见 sold 份额 ~78% > 60%", 16.99, 150, 0.0),
    _vd(53, "DE", "OEM Printer Toner Branded", "排除", "大品牌垄断：单一品牌份额 ~82% > 60%", 24.99, 190, 0.0),
    _vd(54, "US", "Name-Brand Blender Jar Replacement", "排除", "大品牌垄断：头部品牌份额 ~71% > 60%", 19.99, 160, 0.0),
    # ---- §6「品牌官方直营」（Brand outlet / official store，无法竞争）----
    _vd(55, "UK", "Official Brand Store Water Bottle", "排除", "品牌官方直营（official store listing 无法竞争）", 18.99, 240, 0.0),
    _vd(56, "DE", "Brand Outlet Lunch Box", "排除", "品牌官方直营（Brand outlet 直营）", 15.99, 200, 0.0),
    # ---- §5「卖家垄断」（卖家第一名份额 > 50%）----
    _vd(57, "UK", "Single-Seller Dominated Gadget", "排除", "卖家垄断：头部卖家可见 sold 份额 ~63% > 50%", 13.99, 170, 0.0),
    _vd(58, "US", "Monopoly-Seller Kitchen Tool", "排除", "卖家垄断：单一卖家份额 ~57% > 50%", 16.49, 180, 0.0),
    # ---- §5「上架 ≥ 3 年」（太老的红海不做，估算口径）----
    _vd(59, "UK", "Classic Peeler Long-Listed", "排除", "上架 ~4 年 ≥ 3 年（最早评价 Past year 外推估算），红海", 7.99, 160, 0.0),
    _vd(60, "DE", "Old-Model Garlic Press", "排除", "上架 ~5 年 ≥ 3 年（最早购买记录估算），红海", 8.49, 150, 0.0),
    _vd(61, "US", "Legacy Can Opener Steel", "排除", "上架 ~3.5 年 ≥ 3 年（估算），红海竞争", 9.49, 170, 0.0),
    # ---- §7「产品点 ≤ 0.0005」（过了 R1/黑名单，但经济测算不达标）----
    # 这些填算出的产品点（≤ 0.0005），reason 注明口径。低竞品价 × 低月销所致。
    _vd(62, "UK", "Low-Margin Spatula Thin", "排除", "产品点 0.0004 ≤ 0.0005（竞品价低×月销薄，沙箱测算）", 6.49, 14, 0.0004),
    _vd(63, "DE", "Weak-Demand Measuring Jug", "排除", "产品点 0.0004 ≤ 0.0005（经济测算不达标）", 5.99, 12, 0.0004),
    _vd(64, "UK", "Thin-Margin Egg Separator", "排除", "产品点 0.0003 ≤ 0.0005（竞品价 £5.2×月销 10）", 6.20, 10, 0.0003),
    _vd(65, "DE", "Marginal Lemon Squeezer", "排除", "产品点 0.0005 ≤ 0.0005（严格大于才采集，不含等于）", 5.50, 16, 0.0005),
    _vd(66, "US", "Low-Score Bottle Brush", "排除", "产品点 0.0004 ≤ 0.0005（竞品价 $5.1×月销 12，沙箱测算）", 6.49, 12, 0.0004),
    _vd(67, "US", "Underperforming Jar Opener", "排除", "产品点 0.0003 ≤ 0.0005（经济测算不达标）", 5.99, 9, 0.0003),

    # ========== 补采：数据缺失 / 抓取失败（SOP §3 末 / §8）==========
    _vd(68, "UK", "Bamboo Cutlery Travel Set", "补采", "竞品最低价缺失，需补采后再判（疑似有同款未抓到）", 10.99, 0, 0.0),
    _vd(69, "DE", "Reusable Produce Bags 9pc", "补采", "月销无任何销量信号抓取失败，需补采", 9.49, 0, 0.0),
    _vd(70, "UK", "Silicone Stretch Lids 6pc", "补采", "竞品价格页加载失败，待补采", 8.99, 0, 0.0),
    _vd(71, "DE", "Cat Window Perch Hammock", "补采", "变体规格数据缺失，需补采", 16.99, 0, 0.0),
    _vd(72, "UK", "Dog Seat Belt Harness", "补采", "认证信息未抓到，待补采核验", 11.49, 0, 0.0),
    _vd(73, "DE", "Garden Trowel Ergonomic", "补采", "月销与竞品价均缺失，需补采", 9.99, 0, 0.0),
    _vd(74, "UK", "Spice Rack Wall Mounted", "补采", "主图与变体缺失，待补采", 17.99, 0, 0.0),
    _vd(75, "DE", "Car Trash Can Leakproof", "补采", "竞品最低价缺失，需补采后再判", 10.49, 0, 0.0),
    _vd(76, "UK", "Eyebrow Stencil Kit 24pc", "补采", "月销数据抓取失败，待补采", 7.99, 0, 0.0),
    _vd(77, "DE", "Baby Bottle Drying Rack", "补采", "认证与材质信息缺失，需补采", 13.99, 0, 0.0),
    _vd(78, "UK", "Desk Cable Management Tray", "补采", "竞品价差无法测算，待补采", 12.49, 0, 0.0),
    _vd(79, "DE", "Foam Grip Pull-Up Bar", "补采", "变体与月销缺失，需补采", 18.99, 0, 0.0),
    _vd(80, "UK", "Collapsible Laundry Basket", "补采", "竞品页反爬拦截（Checking your browser 未放行），待补采", 14.99, 0, 0.0),
    _vd(81, "DE", "Magnetic Knife Strip 40cm", "补采", "月销与评分数据缺失，需补采", 16.49, 0, 0.0),
    _vd(82, "UK", "Pet Grooming Scissors Curved", "补采", "认证信息未抓到，待补采核验", 10.99, 0, 0.0),
    # 美国站补采
    _vd(83, "US", "Collapsible Silicone Food Bowl", "补采", "竞品最低价缺失，需补采后再判", 11.99, 0, 0.0),
    _vd(84, "US", "Pet Seat Cover Waterproof", "补采", "月销与认证信息未抓到，需补采", 24.99, 0, 0.0),
    _vd(85, "US", "Magnetic Spice Tins Set", "补采", "变体规格数据缺失，待补采", 18.99, 0, 0.0),
    _vd(86, "US", "Desk Pen Holder Rotating", "补采", "竞品页反爬拦截，待补采", 15.99, 0, 0.0),
]


# ---- 幂等：查已存在的 asin_or_url ----

def _existing_urls() -> tuple[set[str], set[str]]:
    cand = {r["asin_or_url"] for r in _db("SELECT asin_or_url FROM product_candidates")}
    vd = {r["asin_or_url"] for r in _db("SELECT asin_or_url FROM selection_verdicts")}
    return cand, vd


# ---- 灌库 ----

def seed(no_spread: bool) -> None:
    have_cand, have_vd = _existing_urls()
    c_added = c_skip = v_added = v_skip = 0

    # 候选档案 + 配套的"采集"判定（同 asin_or_url）
    for c in CANDIDATES:
        url = c["asin_or_url"]
        if url in have_cand:
            c_skip += 1
        else:
            _writer("save_candidate", c)
            c_added += 1
        # 配套采集判定：候选表与判定表各自去重
        if url in have_vd:
            v_skip += 1
        else:
            _writer("save_verdict", {
                "site": c["site"],
                "asin_or_url": url,
                "product_title": c["product_title_en"],
                "verdict": "采集",
                "reason": c["ai_summary"][:200],
                "current_price": c["current_price"],
                "monthly_sales": c["monthly_sales"],
                "product_score": c["product_score"],
            })
            v_added += 1

    # 纯判定流水（PASS / 排除 / 补采）
    for v in EXTRA_VERDICTS:
        if v["asin_or_url"] in have_vd:
            v_skip += 1
            continue
        _writer("save_verdict", v)
        v_added += 1

    print(f"  候选档案：新增 {c_added}，跳过已存在 {c_skip}")
    print(f"  判定流水：新增 {v_added}，跳过已存在 {v_skip}")

    if no_spread:
        print("  （--no-spread：跳过时间打散，所有新数据 created_at 停在今天）")
        return
    _spread_created_at()


def _spread_created_at() -> None:
    """把本批刚灌入（最近 1 小时内）的行的 created_at 打散到最近 SPREAD_DAYS 天。
    只动最近 1 小时窗口，近似隔离本次 seed 批次，不误伤真实爬取/历史数据。"""
    for table in ("product_candidates", "selection_verdicts"):
        _db(
            f"UPDATE {table} "
            f"SET created_at = NOW() - (random() * :span || ' days')::interval "
            f"WHERE created_at >= NOW() - interval '1 hour'",
            {"span": SPREAD_DAYS},
        )
    print(f"  时间已打散：本批数据 created_at 均匀分布到最近 {SPREAD_DAYS} 天")


# ---- wipe：只清本脚本预置的数据（按 asin_or_url 精确匹配）----

def wipe() -> None:
    cand_urls = [c["asin_or_url"] for c in CANDIDATES]
    vd_urls = [c["asin_or_url"] for c in CANDIDATES] + [v["asin_or_url"] for v in EXTRA_VERDICTS]
    _db("DELETE FROM product_candidates WHERE asin_or_url = ANY(:urls)", {"urls": cand_urls})
    _db("DELETE FROM selection_verdicts WHERE asin_or_url = ANY(:urls)", {"urls": vd_urls})
    print(f"  已清除预置数据：候选 {len(cand_urls)} 个 URL、判定 {len(vd_urls)} 个 URL（真实爬取数据不动）")


# ---- 连通性预检 ----

def _preflight() -> None:
    try:
        _db("SELECT 1")
    except Exception as e:
        sys.exit(
            f"❌ 无法连通数据面 Lambda（{DB_LAMBDA} @ {REGION}）：{e}\n"
            "   检查 AWS_PROFILE / AWS_REGION 是否对齐 Part 1 部署（中国区 cn-northwest-1），"
            "以及 Lambda 名是否正确（WRITER_LAMBDA_NAME / DB_LAMBDA_NAME 可覆盖）。"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="预置选品演示数据（反爬兜底）")
    parser.add_argument("--wipe", action="store_true", help="清掉本脚本预置的数据后退出")
    parser.add_argument("--no-spread", action="store_true", help="只灌入，不打散 created_at（不依赖 DB Lambda 的 UPDATE）")
    args = parser.parse_args()

    _preflight()

    if args.wipe:
        print(f"==> 清除预置数据（writer={WRITER_LAMBDA}, db={DB_LAMBDA} @ {REGION}）...")
        wipe()
        return

    print(f"==> 预灌选品数据（writer={WRITER_LAMBDA}, db={DB_LAMBDA} @ {REGION}）...")
    print(f"    计划：{len(CANDIDATES)} 候选 + {len(CANDIDATES)} 采集判定 + {len(EXTRA_VERDICTS)} 纯判定")
    seed(args.no_spread)
    print("✅ 完成。Part 2 门户登录 analyst，问「看下最近 30 天的选品漏斗」即可见数据。")


if __name__ == "__main__":
    main()
