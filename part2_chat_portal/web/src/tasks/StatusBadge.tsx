// 状态徽标：运行状态与任务状态各一套配色（样式见 styles.css 任务模块段）。
import { RUN_STATUS_TEXT, TASK_STATUS_TEXT, type RunStatus, type TaskStatus } from "./api";

export function RunBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`badge run-${status}`}>
      <i className="badge-dot" />
      {RUN_STATUS_TEXT[status]}
    </span>
  );
}

export function TaskBadge({ status }: { status: TaskStatus }) {
  return <span className={`badge task-${status}`}>{TASK_STATUS_TEXT[status]}</span>;
}
