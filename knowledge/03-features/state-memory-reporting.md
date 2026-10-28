# 状态容器 + 任务驱动方式（门禁变更后）

> 门禁只在 VLM 确认的过渡窗调 omni：进入/退出状态、喝水、玩手机、离开等。
> 中间状态不变的窗最多走到本地 VLM 就被拦掉，不调 omni、也不给规则/任务喂
> 时间样本。任务必须只靠「状态边沿 + 定时器/record」推进。

## 状态容器做什么

```text
omni 在过渡窗返回结果
  └─ 回填状态容器：本窗下发的规则哪些命中、有没有 suggestion
       ├─ 有命中/信号 → 视为当前状态，重新计时 5 分钟
       └─ 连续 2 次复核都没有 → 清空该相机状态

门禁拦截的窗
  ├─ 状态还活着 → 刷新建议链 TTL（链不因没有 omni 心跳而饿死）
  └─ 不产生任何规则续命 / 时间样本

每 recheck_interval_sec（默认 300s）
  └─ 状态还活着的相机强制放一窗给 omni 复核
       ├─ 仍命中/仍有信号 → 状态继续
       └─ 连续 2 次都没命中 → 状态结束
```

容器只回答「当前有没有状态、要不要复核」，**不参与任务计时**。

## 任务怎么推进

| 任务类型 | 驱动方式 | 关键点 |
|---|---|---|
| `progress`（8 杯水、次数） | enter 规则 ENTERED → agent `progress-inc` | 事件结束后规则必须 EXIT 才能再次 ENTER；感知过渡窗一帧确认 |
| `event`（活动明细、进出计数） | enter 规则 ENTERED → agent `event-append` | 同上 |
| `duration` 累计（学习 2 小时） | session ENTER/EXIT + agent `session-start/end` + `RecordSource` 定时器 | 计时是墙上时钟 + in-flight，不读每窗样本；退出宽限 60s；5 分钟复核兜底漏退出 |
| 连续状态目标（工作 30 分钟） | session 规则 + `duration_seconds` 会话目标定时器 | 进入后起定时器，按净在状态时长走；宽限内暂停/恢复，真退出取消；每 N 秒重复 `ON_TARGET` |
| milestone（累计达标） | `RecordSource` + record 条件规则 | 不依赖门禁 |
| iot / record 源规则 | iot / DB | 不依赖门禁 |
| cron / on-demand | 独立入口 | 不依赖门禁 |

### 感知过渡窗的 False：一帧确认

感知只在 VLM 确认的过渡窗调 omni，一帧 False 就是真过渡。client 对未命中
规则喂 False 时带 `skip_flicker=True`，由规则/任务的 `exit_debounce_seconds`
提供 1 分钟宽限：

- 60s 内回到态内 → ENTERED 取消退出，会话/定时器继续；
- 60s 没回来 → 真 EXITED，会话结束、定时器取消。

门禁本身不参与这个判断。

### 会话目标定时器（`duration_seconds`）

仅 `direction=session`。进入会话后从 0 起 N 秒定时器：

- 条件变 false → 暂停计时（进入退出宽限），保留剩余时间；
- 宽限内恢复 → 按剩余时间继续，不重置；
- 真退出 → 取消；下次进入重新从 0 计时；
- 到点 → `ON_TARGET`；会话仍在就继续排下一轮（“不是只有一次”）。

不读每窗 True/False 样本。`duration_ratio` 已废弃，仅为库兼容保留。

## 状态记忆 / 上报

- 新建议链报一次；心跳不复报；
- 门禁拦截窗由容器刷新链 TTL，`tick_state` 到点发提醒；
- 全 room 被拦时 client 仍派发 state reminder；
- 5 分钟复核保持链/状态活着；
- 提醒间隔沿用 urgency：high 60s / medium 120s / low 300s。

## 语义与边界

- 状态结束：5 分钟复核连续 2 次无命中/信号。
- 任务计时：不再由状态容器喂样本；中断宽限由 `exit_debounce_seconds` 提供。
- 累计 duration 的 exit 漏检：5 分钟复核补退出，最多多算约 5 分钟 + 宽限。
- 事件/计数：事件结束没被 VLM 抓到 → 5 分钟复核补 False；两次事件在复核
  窗口内合并是已知代价。
- 不做 2% 帧差；不做 restart 恢复（按约定）。
- on-demand 不走状态记忆。

## 配置入口

- `perception.engine.state_memory.enabled=true`
- `perception.engine.state_memory.recheck_interval_sec=300`
- `perception.engine.gate.semantic.mode=enforce`
- `perception.engine.gate.semantic.min_score=0.35`
- `perception.engine.gate.semantic.end_min_score=0.1`
- `perception.engine.gate.semantic.physio_min_score=0.1`
- `perception.engine.gate.hold_duration_sec=0`
- `perception.engine.gate.change_threshold=0.002`

## 测试

- `tests/perception/engine/test_state_memory.py`：命中/未命中、复核到点、
  连续 2 次 miss 清空、禁用。
- `tests/perception/engine/test_suggestion_chain.py`：拦截窗续命后链不被
  TTL 淘汰；旧链（无近期心跳）不被续命；`_merge_results` 不再产规则续命。
- `tests/perception/engine/gate/test_semantic_gate.py`：`force_pass` 复核窗
  绕过视觉/语义门禁。
- `tests/test_perception_client_rule_dispatch.py`：过渡窗 False 带
  `skip_flicker=True`；skipped 路径派发到点提醒。
- `tests/test_task_state_machine_wiring.py`：会话目标定时器的启动/暂停/恢复/
  取消/重复触发。

## 处理链路：门禁先行 + omni 消费循环

采集按 4s 切窗。窗口从缓冲区取出后拆成两个阶段，**门禁不再排在 omni 后面**：

```text
gate 阶段（主 loop，processor.process_realtime）
  collect_batch → 一级 + 二级门禁（engine.gate_batch）
      ├─ 门禁结果立刻写看板内存环（不等 omni，pending 标记）
      ├─ 状态容器到点复核在这里判定（force_pass）
      └─ 上下文 / 规则映射 / 尾音在这里一次性准备好
  → 整窗入 omni 队列（maxsize=2，满时丢最旧一窗，看板仍有门禁结果）

omni 阶段（独立消费循环，_omni_consumer_loop）
  identity → omni（复用 gate 阶段的 packet/timing/snapshot，不重跑门禁）
      ├─ 没过门禁的设备直接返回：不调 omni、不建 identity
      └─ omni 结论回填内存环（pending → called）
```

- 门禁/omni 两阶段共享引擎状态（pending_speech、suggestion 链、状态容器），
  `_state_lock` 只保护 `_prepare_batch` 与 `_merge_results`/状态提醒两个短临界区，
  不跨 await 持有。
- traces 仍按窗发布：gate timing 由 omni 阶段用 `_write_gate_timing` 补写，
  跑分与拦截统计口径不变。
- 采集缓冲（`collect.max_windows` / `full_action`）保持默认 `3 / clear`，不做调参；
  丢窗是否减少取决于门禁阶段能否持续快于窗口产出。
