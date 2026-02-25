import { useEffect, useRef } from "react";
import "./LogPanel.css";

/**
 * 根据事件类型返回对应的图标和颜色类名
 */
function getEventStyle(type) {
  switch (type) {
    case "topology":   return { icon: "🗺️", cls: "log-topology" };
    case "agent_start": return { icon: "⚙️", cls: "log-start" };
    case "agent_done":  return { icon: "✅", cls: "log-done" };
    case "done":        return { icon: "🏆", cls: "log-finish" };
    case "error":       return { icon: "❌", cls: "log-error" };
    default:            return { icon: "ℹ️", cls: "log-info" };
  }
}

/**
 * 将单条事件对象格式化为可读文本
 */
function formatEventMessage(log) {
  switch (log.type) {
    case "topology": {
      const allNodes    = log.nodes ?? [];
      const activeCount = allNodes.filter((n) => !n.is_excluded).length;
      const exclCount   = allNodes.filter((n) =>  n.is_excluded).length;
      const activeEdges = (log.edges ?? []).filter((e) => !e.is_excluded).length;
      return (
        `Topology ready: ${activeCount} active nodes, ${activeEdges} active edges` +
        (exclCount > 0 ? ` (+${exclCount} excluded, no data path)` : "")
      );
    }

    case "agent_start":
      return `Agent [${log.comid}] 开始推理...`;

    case "agent_done": {
      const result = log.result;
      if (!result) return `Agent [${log.comid}] 完成（无结果）`;
      const paramStr = result.parameters
        ?.map((p) =>
          p.estimate_value !== null
            ? `${p.name}: ${p.estimate_value} ${p.unit || ""}`
            : `${p.name}: N/A`
        )
        .join(" | ");
      return `Agent [${log.comid}] 完成 → ${paramStr || "无参数"} | 置信度: ${((result.confidence || 0) * 100).toFixed(0)}%`;
    }

    case "done":
      return `✔ 全部推理完成，目标节点: ${log.target_id}`;

    case "error":
      return `错误: ${log.message}`;

    default:
      return log.message || JSON.stringify(log);
  }
}

/**
 * 日志面板组件
 * 
 * Props:
 *   logs   - 日志列表，每条包含事件数据 + timestamp 字段
 *   status - 当前状态 idle | running | done | error
 */
export default function LogPanel({ logs, status }) {
  // 用于自动滚动到最新日志
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  return (
    <div className="log-panel">
      {/* 头部 */}
      <div className="panel-header">
        <span className="icon">📡</span>
        <h2>实时推理日志</h2>
        <span className={`log-status-dot dot-${status}`} title={status} />
      </div>

      {/* 日志列表 */}
      <div className="log-list">
        {logs.length === 0 ? (
          <div className="log-empty">等待分析开始...</div>
        ) : (
          logs.map((log, i) => {
            const { icon, cls } = getEventStyle(log.type);
            return (
              <div key={i} className={`log-entry ${cls}`}>
                {/* 时间戳 */}
                <span className="log-time">{log.timestamp}</span>

                {/* 图标 */}
                <span className="log-icon">{icon}</span>

                {/* 主要消息文本 */}
                <span className="log-msg">{formatEventMessage(log)}</span>

                {/* 如果是 agent_done，展示分析文本（可折叠）*/}
                {log.type === "agent_done" && log.result?.analysis && (
                  <details className="log-detail">
                    <summary>查看分析详情</summary>
                    <p>{log.result.analysis}</p>
                  </details>
                )}
              </div>
            );
          })
        )}
        {/* 滚动锚点 */}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
