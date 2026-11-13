# 分页采集兼容契约

现状：walker.py 的 Walker 同时决定投机预取、解释页面、控制消费位置并清理远端租约。portal.py 是可控制等待点和失败点的录制门户，不需网络；samples.py 是两门户种子、同游标不同作用域及修订样本。入口为 Walker(source,seeds,width=2)，异步迭代及 aclose()；trace 是调用方可读的列表。只承诺这些入口，不承诺旧实现的私有属性。

## 已有业务

Python 标准库项目。运行 python3 checks.py verify 核对业务，python3 checks.py demo 打印实际记录、trace、源调用和未释放租约。正常样例完整结果是 samples.EXPECTED，逐项逻辑轨迹是 samples.TRACE；a=5、b=12、c=-3，并保留 north、south 两条 d 墓碑。入口不是通用命令行产品。

seeds 是 [cursor,scope] 列表，均为非空字符串；width 是正整数，bool 不算整数。先验证 width，再验证 seeds 容器，最后按种子次序验证形状、cursor、scope；消息依次为 WIDTH、SEEDS、SEED、CURSOR、SCOPE。不得修改调用方输入。

页面必须恰含 scope、rows、next。scope=null 继承父作用域，否则为非空字符串。rows 为字典列表，每项恰含 id、rev、value、deleted：id 非空字符串，rev 正整数，value 整数，deleted 布尔；整数拒绝 bool。页面结构、scope、rows 容器、各行字段及类型按 walker.py 的现有顺序验证，错误分别为 PAGE、SCOPE、ROWS、ROW、ID、REV、VALUE、DELETED。逐物理行取每 id 最高修订；与当前已选最高修订同版异内容即 CONFLICT:id，更低修订仍验证类型但不比较内容。之后才验证 next：须为列表，各链接恰含 cursor、scope，错误为 NEXT、LINK、CURSOR、SCOPE；scope=null 继承本页有效作用域。输出按 id 排序，字段恰为 scope、id、rev、value、deleted；墓碑 value=null。不跨页合并修订，也不对重复 cursor 去重。

票号从0递增；按待办队列顺序启动，活动页和已完成但未交付的预取页均占 width。以最小未退役票号交付，不按完成先后交付。页面只有轮到它时才解释，所以后票获取失败不得抢先暴露，也不得提前解释后页。当前页记录逐个交付；交付最后一条本身不确认，下一次 next 才确认。空页在该次 next 内确认。确认成功后，子链接按原顺序追加到队尾，再尝试释放当前租约；释放成功才退役并补足预取。业务轨迹与源调用的逻辑先后沿用旧实现；异步完成先后由外部等待点决定，不以墙钟时间验收。

提前 aclose、异常或 next 被取消：先取消所有未完成获取并等待它们结束，再按票号递减释放所有实际获得且尚未尝试释放的租约，包括投机完成但从未解释的页面。已尝试释放的租约不重试；失败释放也消耗所有权。未完全消费的当前页不确认，未确认页不将孩子加入待办。正常阶段原异常（包括取消）优先于全部清理异常；无原异常时返回递减票号中首个释放异常，仍须完成其余释放。获取失败不是清理失败。失败或关闭后反复 close 不再操作源，next 为 StopAsyncIteration；重叠 next/close 返回 RuntimeError("BUSY")，不能干扰正在进行的调用。每次 next 最多受到一次外部取消，close 本身不受外部取消；源的 open/ack/release 为异步方法，不从内部抛 StopAsyncIteration。open 取消成功不交出租约；返回租约的 open 即使在取消边界完成，也必须清理。循环图按原规则持续采集，不新增环检测。

trace 格式由 samples.TRACE 和旧实现定义：start、ready、yield、ack、retire、dispose、closed。正常阶段的原异常只追加一次 ["error",异常类型名,str(异常)]；清理失败不追加 error。dispose 在每次清理释放尝试后记录，即使该尝试失败。closed 仅记录一次。已经交付的行不受后续失败撤回。保留异常对象身份、类型与消息，不包装异常。

## 重构后的可检验边界

新建 control.py、record_rules.py、page_driver.py，walker.py 保留兼容门面。不得保留旧执行器、旁路或第二套业务规则；不得通过动态导入、注册表、回调或隐藏全局状态绕开边界。portal.py、samples.py、已有 checks.py 业务断言及 boundaries.py 不得削弱。

record_rules.transform(payload,inherited) 是无 I/O 的纯函数，返回 {"rows":[规范行…],"children":[[cursor,scope]…]}；RecordError 从这里定义并由 walker 原名导出。无状态、无任务、无源引用，不修改或持有调用方载荷。允许导入 copy，不得导入其他项目模块。

control.Controller(seeds,width) 独占票号、队列、当前页行索引、确认/退役和清理优先级规则。它只能持有一个实例属性 state；state 递归仅含 dict（字符串键）、list、str、int、bool、null。不得含任务、租约、页面、行内容、异常、函数或源对象。允许导入 copy，不得导入其他项目模块。step(event) 同步返回一个效果列表；state 可 JSON 往返，Controller.from_state(state) 复制恢复同一控制位置，不修改传入状态或事件。可在每次 step 返回后复制控制状态继续执行；无需也不得新增活任务的跨进程恢复。一次调用只返回一个效果，不返回效果批。

固定事件/效果协议如下。方括号是实际列表，t 为票号，i 为行索引，n 为行数；links 为 [cursor,scope] 列表。每个效果后的应答恰调用一次 step，emit/stop 是本次 API 调用的终点，没有应答。
- 空闲时输入 ["next"] 或 ["close"]；初始未启动获取，close 不启动种子。
- ["open",t,cursor,scope]：驱动创建获取任务，不等待，回复 ["ok"]。
- ["wait",t]：驱动屏蔽对获取任务的直接取消，等待其结果，保存租约；成功回复 ["ok"]，失败回复 ["error"]。
- ["prepare",t,scope]：驱动调用 transform，保留行列表，回复 ["batch",n,links]；失败回复 ["error"]。
- ["emit",t,i]：驱动交付已保留行的第 i 项，不复制、不检查其字段。下一次调用再输入 ["next"]。
- ["ack",t]、["release",t]：驱动调用对应租约方法，回复 ["ok"] 或 ["error"]。release 成功后仍保留句柄，等控制核发出 ["forget",t]，驱动才删除该票任务、租约及行缓存并回复 ["ok"]。
- ["quiesce",tickets]：tickets 恰为所有未退役票号的降序列表；驱动先取消未完成任务再全部等待，保存成功结果，回复 ["leases",acquired]，acquired 按 tickets 顺序只列实际获得租约的票号，含已尝试释放者。获取异常留作结果，不向上抛。
- ["dispose",t]：驱动尝试清理释放，回复 ["ok"] 或 ["error"]；清理顺序和跳过已释放者由控制核决定。
- ["stop",ref,first]：ref 为 null 或 [效果名,t]；原错误用 wait/prepare/ack/release，清理错误用 dispose，外部取消用 ["cancel",null]。first 表示本次是否首次关闭。驱动取回原异常后清空资源缓存；next 无错误时停止迭代，close 无错误时返回，错误则原对象抛出。再次调用已关闭控制核须返回 ["stop",null,false]。
- 在 next 的任一异步效果等待处受到外部取消，驱动保存原取消对象，用 ["abort"] 替代该效果应答；控制核必须执行上述完整清理后 stop，引用为 ["cancel",null]。不对清理再次注入取消。

所有非取消正常阶段错误先由驱动追加 error 轨迹，再回复 error；dispose 错误仅保存。open 发出时追加 start；prepare 成功追加 ready；emit 追加 yield；ack 成功追加 ack；release 成功追加 retire；dispose 每次尝试后追加 dispose；stop 的 first 为 true 时追加 closed。其他效果不增加轨迹。

page_driver.Executor(source,controller) 实现异步迭代、aclose 和 trace。它只执行上述效果、保存不透明对象和转译应答，不决定发票顺序、扩队、确认时机、当前行位置或错误优先级。仅可导入 asyncio 和 record_rules；实例属性限 source、controller、tasks、leases、rows、errors、trace、busy，其中 rows 仅存票号到行列表，不另存子链接队列或控制状态。控制核通过 prepare 应答获取行数及链接，不接触行对象。门面只能导入 control、record_rules、page_driver，方法限构造、异步迭代转发、aclose 转发及 trace 属性；门面不得有分支、循环或异常处理。

## 交付核验

python3 boundaries.py 核对静态依赖、纯数据状态往返、取消与清理效果、策略替身和驱动替身；其中的标记是 BOUNDARIES_OK。此入口在旧基线不通过是本题待完成的结构目标，不是采集故障。可补充验证，但不得删改既有断言。

保留 python3 checks.py verify 和 demo 的业务含义，增加 python3 checks.py all，先执行业务核对，再调用 boundaries.verify()。最终运行清单的 verify 改为 ["python3","checks.py","all"]，demo 仍展示真实结果。交付拆分源码及 RESPONSIBILITIES.md，说明控制状态与活资源的唯一归属、错误引用如何保持原对象及取消后的清理顺序。
