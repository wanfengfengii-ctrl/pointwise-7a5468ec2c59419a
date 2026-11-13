# 职责与依赖边界说明

重构把旧 walker.py 的混合职责拆成四层：`record_rules.py`（业务整理）、
`control.py`（调度控制核）、`page_driver.py`（异步驱动）、`walker.py`（兼容门面）。
依赖方向单向：walker → page_driver → record_rules，walker → control；
control 与 record_rules 不导入任何项目模块。

## 每类状态的唯一归属

| 状态类别 | 唯一归属 | 形态 |
| --- | --- | --- |
| 页面解释规则（校验、修订取舍、规范化） | `record_rules.transform` | 无状态纯函数，不修改也不持有调用方载荷 |
| 票号、待办队列、当前页行索引、确认/退役时机、清理优先级 | `control.Controller.state` | 唯一实例属性；递归仅含 dict/list/str/int/bool/null，可 JSON 往返，`from_state` 复制恢复 |
| 获取任务（task） | `page_driver.Executor.tasks` | 活资源，不透明，不入控制核 |
| 远端租约（lease 句柄） | `page_driver.Executor.leases` | 活资源；release 成功后保留句柄直到 `["forget",t]` |
| 已整理行载荷 | `page_driver.Executor.rows` | 仅存票号→行列表；子链接经 prepare 应答交给控制核，驱动不另存队列 |
| 原异常对象 | `page_driver.Executor.errors` | 以 `[效果名,票号]` 为键保存原对象 |
| 调用方轨迹、占用标记 | `page_driver.Executor.trace` / `busy` | trace 是调用方可读列表；BUSY 只挡重叠 next/close |
| 兼容入口（构造、异步迭代、aclose、trace） | `walker.Walker` | 仅转发，无分支/循环/异常处理 |

控制核的 `state` 不含任务、租约、页面、行内容、异常、函数或源对象；
驱动不决定发票顺序、扩队、确认时机、当前行位置或错误优先级；
每次 step 返回后都可用 `from_state` 复制控制状态继续执行（不恢复活任务）。

## 错误引用如何保持原对象

控制核只持有可 JSON 往返的引用：`["wait",t]`、`["prepare",t]`、
`["ack",t]`、`["release",t]` 表示正常阶段原错误，`["dispose",t]` 表示清理
错误，`["cancel",null]` 表示外部取消。驱动在对应效果失败时把**原异常对象**
存入 `errors`（非取消错误先追加 `["error",类型名,str(异常)]` 轨迹再回复
error；dispose 失败仅保存不追加轨迹）。`["stop",ref,first]` 到达时，驱动按
ref 取回原对象、清空资源缓存，然后把**同一对象**原样抛出——不包装、不复制，
`is` 身份、类型与消息全部保留。已交付的行不被后续失败撤回。

## 取消后的清理顺序

next 在任一异步效果等待处受到外部取消（每次 next 至多一次，close 不受
外部取消）时：驱动保存原取消对象，以 `["abort"]` 替代该效果应答；控制核
随后执行完整清理——

1. `["quiesce",降序票号]`：驱动先取消所有未完成获取任务，再等待全部结束；
   获取异常（含被取消的 open）留作结果不上抛，成功租约（含取消边界才完成
   的 open）全部保留；
2. `["dispose",t]`：按票号递减逐个清理实际获得且**尚未尝试释放**的租约；
   已尝试释放者（含被取消或失败的 release）消耗了所有权，绝不重试；
   某项 dispose 失败仍继续其余项；
3. `["stop",ref,first]`：原异常（含取消）优先于全部清理异常；无原异常时
   取递减票号中首个 dispose 失败。closed 轨迹只记一次，之后反复
   next/close 只得到 `["stop",null,false]`，不再操作源。

不新增落盘恢复：控制状态可 JSON 复制续跑，活任务/租约不做跨进程恢复。
