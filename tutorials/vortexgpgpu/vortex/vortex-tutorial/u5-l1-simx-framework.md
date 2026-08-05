# SimObject/SimChannel/SimPlatform 基元

## 1. 本讲目标

Vortex 的 SimX 是一个**周期精确（cycle-accurate）**的 C++ 仿真器，它要成为 RTL 的「预言机（oracle）」——同一段程序在 SimX 和 RTL 上退休的指令必须逐条一致、周期误差在容差内（见 u1-l1 提到的 model_parity）。要支撑这种精度，SimX 不能随便写「面向对象 + 调用函数」，它必须有一套**带时间维度**的建模框架。

这套框架就藏在 `sim/common/simobject.h` 一个头文件里。学完本讲，你应当能够：

1. 说出 `SimObject<Impl>`、`SimChannel<Pkt>`、`SimPlatform` 三个基元各自的职责，以及它们如何组成一个可仿真的模块。
2. 解释 SimX 的「时间」是怎么被推进的：`SimPlatform::tick()` 每调一次前进一个周期，通道的 `delay` 决定数据要「走几个周期」。
3. 理解 **「channel 就是流水线」** 这条核心直觉：为什么一个多级流水线单元**不需要**在内部维护一个 `std::deque` 来当级间寄存器。
4. 读懂一条 `send` 数据从「被发送」到「被消费」之间在框架内部经历了什么（事件、时间轮、背压）。
5. 掌握贯穿全栈的**基数规则（Cardinal Rule）**：模块之间只通过 channel 通信，绝不跨所有权层级直接抓别的对象。

本讲是整个 U5「SimX 模拟器框架」单元的地基，后续 u5-l2（处理器层次）、u5-l3（基数规则与模块分解）都建立在这三个基元之上。

---

## 2. 前置知识

本讲假设你已经学完 u1-l4（会用 `blackbox.sh` 跑通 SimX），并且对以下概念有基本了解。不熟悉的也没关系，下面用通俗语言补一句：

- **周期精确仿真（cycle-accurate simulation）**：仿真器里有一个「全局时钟」，每「滴答」一下代表真实硬件的一个时钟周期。所有操作都被挂在这个时钟上，能用周期数精确衡量延迟。
- **离散事件仿真（discrete-event simulation）**：不用连续地推进时间，而是把「将来某个周期要发生的事」排成一张事件表，时间一格一格地跳，跳到事件该发生的周期就执行它。SimX 用的是其中的「时间轮（timing wheel）」变体。
- **CRTP（Curiously Recurring Template Pattern，奇异递归模板）**：C++ 里一种写法 `class Derived : public Base<Derived>`，让基类在编译期就知道派生类的真实类型，从而能调用派生类的方法且没有虚函数开销。SimX 用它来实现 `SimObject`。
- **流水线寄存器（pipeline register / latch）**：真实硬件里，流水线两级之间有一排触发器，把上一级这个周期的结果「锁存」住，下一级下个周期才能看到。它就是流水线的「时间墙」。
- **背压（backpressure）**：当下游处理不过来（队列满了）时，向上游传递「别再发了」的信号，避免数据被覆盖或丢失。

一句话建立心智模型：**SimX 把芯片上的「连线」抽象成 `SimChannel`，把「时钟与事件调度」抽象成 `SimPlatform`，把「挂在时钟上的功能模块」抽象成 `SimObject`。** 数据沿着 channel「旅行」，每旅行一个周期，就是流水线里的一级。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [sim/common/simobject.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h) | SimX 仿真运行时的核心头文件，定义三个基元及事件系统 | 全讲主线 |
| [docs/simobject.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md) | 框架的权威设计文档，含基数规则、tick 循环、常见模式 | 概念直觉来源 |
| [sim/simx/func_unit.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h) | 真实的功能单元 CRTP 基类，演示三大基元如何被实际使用 | 真实用法锚点 |
| [sim/simx/main.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp) | SimX 可执行文件的入口，含驱动 `tick()` 的主循环 | 看 SimPlatform 如何被驱动 |

> 说明：`simobject.h` 用尖括号 include 了 `linked_list.h`、`mempool.h`、`util.h`、`smallfunc.h`、`ringqueue.h` 等辅助工具头（时间轮用的侵入式链表、对象池分配器、定长函数对象、环形队列）。它们是性能优化零件，不是本讲的认知重点，我们只在用到时点名其作用。

---

## 4. 核心概念与源码讲解

本讲按「三个基元 + 一条铁律」拆成 4 个最小模块：

- 4.1 `SimObject<Impl>`：CRTP 模块基元（一个可被时钟驱动的模块长什么样）
- 4.2 `SimChannel<Pkt>`：带延迟与背压的通道（数据如何「旅行」）
- 4.3 `SimPlatform`：时间轮 + delta 事件的时序引擎（时间如何被推进）
- 4.4 基数规则：模块只通过 channel 通信（为什么这是不可逾越的红线）

---

### 4.1 SimObject\<Impl\>：CRTP 模块基元

#### 4.1.1 概念说明

一个 SimX 模块（比如一个 ALU、一个缓存、一个调度器）要能被仿真，必须满足两件事：

1. **每个周期能被叫醒一次**，干一点活（取指、运算、搬运）——这叫 `on_tick()`。
2. **能被复位**，把内部状态清零——这叫 `on_reset()`。

`SimObject<Impl>` 就是提供这两个生命周期钩子的 CRTP 基类。你写一个模块时这样继承：

```cpp
class MyUnit : public SimObject<MyUnit> {   // 把自己作为模板参数传给基类
  ...
protected:
  void on_tick();   // 每周期由 SimPlatform 调用
  void on_reset();  // 复位时由 SimPlatform 调用
};
```

CRTP 的妙处在于：基类 `SimObject<MyUnit>` 在编译期就知道「我底下的派生类是 `MyUnit`」，于是它能用 `static_cast<MyUnit*>(this)` 安全地向下转，去调用你写的 `on_tick()`，而**不需要把 `on_tick` 设成虚函数**（避免每周期一次虚调用的开销）。框架对外暴露的是非虚的 `do_tick()`，内部再转发到你具体的 `on_tick()`。

还有两个反直觉但重要的设计点：

- **构造函数是私有的「闸门」**：你不能 `new MyUnit(...)` 或 `MyUnit u(...)`。只能用 `MyUnit::Create(...)` 工厂方法，它会把对象登记到 `SimPlatform`，这样平台才知道「这个模块存在、每个周期要叫醒它」。
- **空 `on_tick` 不花一分钱**：如果一个模块只是「拥有几个 channel 当管道」、本身不需要每周期干活，框架会**自动检测**它没重写 `on_tick()`，从而**不把它放进每周期循环**。这就是 simobject.md §2 说的「auto-skip for passive SimObjects」。

#### 4.1.2 核心流程

一个模块从「被创建」到「每周期被叫醒」的流程：

```text
MyUnit::Create(args...)
   │  转发到
   ▼
SimPlatform::create_object<MyUnit>(args...)
   │  1. std::make_shared<MyUnit>(SimContext{}, args...)  构造对象
   │     （SimContext 是个空结构体的「钥匙」，只有平台能造，
   │       因此外部无法绕过工厂直接构造）
   │  2. objects_.push_back(obj)                            登记到全局对象表
   │  3. 检测是否重写了 on_tick/on_reset：
   │       若重写 → 放进 active_tick_ / active_reset_（每周期/复位时遍历）
   │       若没重写 → 不放，零开销
   ▼
返回 shared_ptr<MyUnit>

每个周期 SimPlatform::tick() 内部：
   for (auto* object : active_tick_)
       object->do_tick();   // 非虚，内部 static_cast<MyUnit*> 后调 on_tick()
```

成员指针检测是关键技巧：框架比较 `&MyUnit::on_tick` 和 `&SimObject<MyUnit>::on_tick` 这两个**成员函数指针**。如果相等，说明 `MyUnit` 没重写、继承的是基类那个空实现；如果不等，说明重写了。这全在编译期 + 创建时完成，运行期零开销。

#### 4.1.3 源码精读

**抽象基类 `SimObjectBase`**——所有模块的根，定义了私有的纯虚 `do_tick`/`do_reset`，只让友元 `SimPlatform` 调用：

[sim/common/simobject.h:L50-L65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L50-L65) —— `SimObjectBase` 用 `SimContext` 钥匙把构造函数收窄，`do_tick`/`do_reset` 是 private 纯虚函数，仅 `friend SimPlatform` 可触发。

**`SimContext`「钥匙」**——一个空类，构造函数私有，只有 `SimPlatform` 和 `SimChannel` 是友元：

[sim/common/simobject.h:L42-L47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L42-L47) —— 这就保证了外部代码拿不到 `SimContext{}` 实例，从而无法绕过 `Create()` 工厂去 `std::make_shared` 一个模块，强制所有对象都经平台登记。

**CRTP 模板 `SimObject<Impl>`**——提供 `Create` 工厂、protected 的空实现钩子、以及转发：

[sim/common/simobject.h:L454-L476](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L454-L476) —— 注意第 470–471 行 `on_tick()`/`on_reset()` 是基类提供的**空默认实现**，派生类重写它们；`do_tick` 在第 476 行 `static_cast<Impl*>(this)->on_tick()` 完成向下转发。

**成员指针检测 `has_own_tick`**——决定模块是否要进每周期循环：

[sim/common/simobject.h:L481-L491](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L481-L491) —— 比较两个成员函数指针：若 `Impl` 重写了 `on_tick`，二者地址不同，返回 `true`。

**工厂 `create_object` + active 登记**——带编译期断言「钩子必须 protected」：

[sim/common/simobject.h:L507-L527](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L507-L527) —— 第 509–512 行 `static_assert` 禁止 `on_tick`/`on_reset` 是 public（只有平台该调它）；第 517–525 行只在 `has_own_tick` 时把对象塞进 `active_tick_`。

**真实用法**——`FuncUnit`（功能单元基类）就是这么继承的，并且演示了 `make_sim_channels` 批量构造 channel 数组：

[sim/simx/func_unit.h:L38-L58](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L38-L58) —— 注意这里把 `SimChannel<instr_trace_t*>` 当成**值成员**直接挂在模块里，并用 `make_sim_channels<instr_trace_t*, NUM_BLOCKS>(this)` 解决「channel 不能默认构造」的问题（channel 构造需要 owner 指针）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「空 `on_tick` 零开销」和「钩子必须 protected」这两条机制。

**操作步骤**：

1. 打开 [sim/common/simobject.h:L454-L494](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L454-L494)，确认 `SimObject<Impl>` 的 `on_tick`/`on_reset` 默认是空函数体 `{}`。
2. 阅读 [sim/common/simobject.h:L507-L527](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L507-L527) 的 `create_object`，理解第 518 行 `has_own_tick<Impl>()` 为 `false` 时对象**不会**被加入 `active_tick_`。
3. （可选，源码阅读型）在头脑中构造两个模块：`class A : public SimObject<A>`（不写 `on_tick`）和 `class B : public SimObject<B>`（写了 `on_tick`）。设想 `A::Create()` 和 `B::Create()` 分别调用后，`active_tick_` 里只有 `B` 的指针。

**需要观察的现象**：理解「被动模块（pure plumbing / facade）」为何不进入每周期循环——它只是用来「拥有 channel、提供名字和拓扑」。

**预期结果**：能用自己的话说明「不要给一个不需要每周期干活的模块写空的 `on_tick()`；要么不写（继承空默认），要么接受它会被每周期调用」。这与 simobject.md §2 的建议一致。

> 待本地验证：若你已在 build 树中编译过 SimX，可尝试在某个 `Create()` 调用处打断点，观察 `active_tick_.size()` 与 `objects_.size()` 的差值——差额就是那些被动模块的数量。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SimObject<Impl>` 要用 CRTP，而不是直接用虚函数 `virtual void on_tick() = 0`？

> **答案**：两个原因。一是性能：SimX 一个周期可能要 tick 上千个对象，每周期一次虚函数派发（vtable 间接跳转）累积成本可观，CRTP 的 `static_cast<Impl*>` 在编译期就解析了调用，没有间接跳转。二是配合「成员指针检测」：CRTP 让基类在编译期知道派生类型，从而能用 `&Impl::on_tick != &SimObject<Impl>::on_tick` 判断是否重写，实现被动模块的零开销跳过。注意对外接口 `do_tick` 仍是非虚的，转发内部用 `static_cast`。

**练习 2**：如果一个模块只重写了 `on_reset()` 没重写 `on_tick()`，它会被加入 `active_tick_` 吗？

> **答案**：不会。`has_own_tick()` 和 `has_own_reset()` 是**独立检测**的（见 L481–L491 与 L517–L521）。只重写 `on_reset` 的模块会进 `active_reset_`（复位时被叫醒），但不进 `active_tick_`（每周期不干活）。这正是「每个钩子各自记账」。

---

### 4.2 SimChannel\<Pkt\>：带延迟与背压的通道

#### 4.2.1 概念说明

如果说 `SimObject` 是「挂在时钟上的模块」，那么 `SimChannel` 就是「连接模块的连线」。但它在三个维度上比一根普通连线更强：

1. **带类型**：`SimChannel<MemReq>` 只能传 `MemReq`，编译期保证不会把响应塞进请求通道。
2. **带延迟**：`send(pkt, delay)` 表示这个包要过 `delay` 个周期才到达对端。`delay=1`（默认）相当于在两级之间插了一级流水线寄存器；`delay=0` 是「同周期立即到达」（组合逻辑直达）。
3. **带背压**：channel 有容量（默认 `capacity=2`）。当下游满了，`full()` 返回 true，生产者就不能再 `send`（断言失败或 `try_send` 返回 false）。

理解 channel 的关键在于它有两种工作模式（这是初学者最容易混淆的点）：

- **端点模式（endpoint）**：channel 自己内部有一个 `RingQueue<Pkt>` 队列当存储。生产者往里 `send`，消费者用 `peek()`（看队首）/`pop()`（弹出）取走。这是「真正的队列」。
- **转发模式（forwarding）**：channel 被 `bind()` 绑到下游另一个 channel 上，自己**不存数据**。事件到达时直接调用下游的 `receive_packet()`。对转发 channel 调 `peek()`/`pop()` 是**运行期断言错误**。

为什么要有转发模式？因为一个模块通常「对外暴露一组输入/输出端口」，这些端口在构造时还不知道接给谁；等拓扑建好后用 `bind()` 把它们串起来。端口本身是转发 channel，真正的存储在链路最末端的那个端点 channel 里。`full()`、`size()` 这些查询会**沿着 bind 链一路问到底**，反映的是末端端点的状态。

#### 4.2.2 核心流程

一个包从 `send` 到被 `pop` 的完整生命周期：

```text
生产者：ch.send(pkt, delay=1)
  1. assert(!ch.full())           // 满了就断言（或用 try_send 得 false）
  2. ch.reserve()                 // 「预约」一个位置
       └─ 若是端点：++pending_count_（在途计数 +1），全局 inflight_count +1
       └─ 若是转发：转发给下游端点去 reserve
  3. SimPlatform::schedule(this, pkt, delay)
       └─ delay>0：把包封成 SimChannelEvent，按 fire_cycle 放进时间轮
       └─ delay=0：放进 imm_events_（本周期 delta 循环里就触发）

—— 时间推进到事件该触发的周期 ——

事件 fire() → channel->receive_packet(pkt)
  1. 若有 tx_callback：先回调（总线监听，可计数/打日志）
  2. 若是转发：若有 convert_fn_ 则转换后送下游，否则直接送下游端点
  3. 若是端点：--pending_count_，包入队 storage_

消费者：ch.peek() 看队首；ch.pop() 弹出
       └─ pop 时 --inflight_count（包彻底离开系统）
```

**背压的关键**：注意第 2 步的 `pending_count_`。一个包从 `send` 到「真正入队」之间，有一段时间它「在时间轮里飞着」，既不在生产者手里、也还没进消费者队列。如果不把这批「在途」的包算进容量，生产者就能一口气 `send` 一堆带延迟的包、把小容量队列塞爆。所以 `full()` 判断的是 `occupancy = queue_size + pending_count`——**已入队的 + 在途的**，二者之和不能超容量。容量在**发送时刻**就被强制。

> 数学上，端点的占用率满足约束：\( \text{occupancy} = \text{queue\_size} + \text{pending\_count} \le \text{capacity} \)。其中 queue_size 是已送达待消费的，pending_count 是已发送未送达的。

#### 4.2.3 源码精读

**`SimChannelBase` 拓扑内省 + 全局在途计数**——所有 channel 的公共基类：

[sim/common/simobject.h:L68-L103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L68-L103) —— 第 73–80 行提供 `module()`/`sink()`/`source()` 拓扑查询；第 83–86 行的 `inflight_count()` 是进程级静态计数器，用于检测「系统里还有没有包在飞」（仿真结束的排空判定、死锁检测）。

**构造与默认容量**——channel 默认容量是 2：

[sim/common/simobject.h:L233-L236](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L233-L236) —— `storage_(capacity)` 是端点的 `RingQueue<Pkt>`，`pending_count_(0)` 是在途计数。

**`full()` / `size()` 沿 bind 链查询**——转发 channel 反映的是末端端点的状态：

[sim/common/simobject.h:L286-L289](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L286-L289) —— 若绑了下游（`sink_` 非空），就问下游的 `full()`；否则看自己的 `occupancy()`。

**`occupancy = queue + pending`**——背压的数学定义：

[sim/common/simobject.h:L392-L392](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L392-L392) —— `occupancy()` = `queue_size() + pending_count_`，正是上面公式里那个约束的左端。

**`send` + `try_send`**——发送即「断言不满 → 预约 → 排程」：

[sim/common/simobject.h:L291-L313](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L291-L313) —— `send` 满了就断言；`try_send` 满了返回 `false` 不抛。二者都先 `reserve()` 再 `schedule()`。

**`reserve` 的端点/转发分支**——在途计数只在端点累加：

[sim/common/simobject.h:L351-L358](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L351-L358) —— 转发 channel 把 `reserve` 透传给下游；端点 channel 才自增 `pending_count_` 和全局 `inflight_count()`。

**端点消费 `peek`/`pop`/`try_pop`**——只能在端点 channel 上调用：

[sim/common/simobject.h:L320-L338](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L320-L338) —— 第 322/328 行对空队列断言；`pop` 在 L397–L400 递减全局 `inflight_count`（包彻底离开系统）。

**`receive_packet`：送达时刻的总调度**——回调、转换、入队都在这里：

[sim/common/simobject.h:L360-L376](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L360-L376) —— 第 361–363 行先跑 `tx_cb_`（总线监听）；第 364–372 行若有下游就转发（可带类型转换）；第 373–375 行端点则递减 `pending_count_` 并入队。

**三种 `bind` 重载**——精确类型、显式转换器、隐式可转换：

[sim/common/simobject.h:L261-L281](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L261-L281) —— 第二种重载（L266–L273）支持一个 `Converter`，在送达时把 `Src` 转 `Dst`（如仲裁器要改 tag、重新打包字段）。绑定是一次性的，重复绑定会断言。

#### 4.2.4 代码实践

**实践目标**：理解端点 channel 与转发 channel 的区别，以及 `delay` 如何制造「流水线级」。

**操作步骤**：

1. 阅读 simobject.md §5 的 `MyFifo` 例子（[docs/simobject.md:L283-L298](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md#L283-L298)）：它每个 tick 从 `Inputs` 取一个包、`send` 到 `Outputs`。
2. 在 [sim/common/simobject.h:L291-L301](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L291-L301) 确认 `send` 默认 `delay=1`，意味着「这个包下一个周期才出现在 `Outputs` 里」。
3. 设想两个 `MyFifo` 串起来：`A.Outputs` bind 到 `B.Inputs`。一个包从 `A.Inputs` 进，需要：A 的 tick 把它送到 A.Outputs（1 周期）→ 下一周期 B 的 tick 才能从 B.Inputs peek 到 → B 再 send 到 B.Outputs（再 1 周期）。两级流水线 = 两个 channel 各贡献 1 周期延迟。

**需要观察的现象**：包「在哪一周期能被看到」完全由 channel 的 `delay` 决定，模块内部的 `on_tick` 不需要维护任何「这个包现在在第几级」的状态。

**预期结果**：能说清「要让数据延迟 N 个周期，就串 N 个 delay=1 的 channel（或 N 个各带 1 周期延迟的单元），而不需要在模块里写 `std::deque<Pkt> stage_registers_[N]`」。

> 待本地验证：可在 SimX 调试模式下（见 u13-l2 的 `--debug`）打印某个 channel 在连续若干周期的 `size()`，观察包「逐级前移」的过程。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `full()` 要算上 `pending_count_`（在途包），而不只看已经入队的 `queue_size`？

> **答案**：因为带 `delay` 的包在 `send` 之后、到达之前，已经「占用了未来的一个位置」。如果 `full()` 只看当前队列长度，生产者就能在同一个周期 `send` 多个延迟包，它们的 `delay` 到期时会同时涌入、撑爆小容量端点。把 pending 也计入占用率，就是在**发送时刻**提前扣减容量，确保 `queue_size + pending_count ≤ capacity` 永远成立。

**练习 2**：转发 channel 和端点 channel 的 `peek()`/`pop()` 行为有何不同？

> **答案**：端点 channel 有内部 `RingQueue`，`peek()`/`pop()` 操作这个队列。转发 channel 自己不存数据（`sink_` 非空），调 `peek()`/`pop()` 会触发 `assert_endpoint()` 断言失败（L388–L390）。转发 channel 的唯一职责是把送达的包转给下游，所以它没有「队首」可看。

**练习 3**：`tx_callback` 和 `bind` 的 converter 都在「送达时刻」运行，它们有何分工？

> **答案**：`tx_callback` 是**旁路监听**（bus snoop），看到包和当前周期，用于计数/打日志/触发副作用，**不改包的流向**。converter 是**转换**，把 `Src` 变成 `Dst` 再送给下游 channel，**改变包的内容和类型**。文档明确建议：要变换包就用 converter 重载的 `bind`，不要用 `tx_callback` 去改包。

---

### 4.3 SimPlatform：时间轮 + delta 事件的时序引擎

#### 4.3.1 概念说明

`SimPlatform` 是 SimX 的「心脏」。它是一个**单例（singleton）**，三件事全归它管：

1. **拥有所有对象**：用 `shared_ptr<SimObjectBase>` 持有每个 `create_object` 出来的模块，负责它们的生命周期。
2. **驱动全局时钟**：`tick()` 每调一次，全局周期数 `cycles_` 加 1。SimX 主程序就是一个 `while` 循环反复调 `tick()`。
3. **排程事件**：所有「将来某周期要发生的事」（channel 送达、延迟回调）都被它排进事件表。

事件表用了一个经典数据结构——**时间轮（timing wheel）**。想象一个有 4096 个槽的轮盘，第 `c` 个周期该发生的事件就挂在槽 `c mod 4096` 上。每个 `tick()` 结束时，看一眼当前周期对应的槽，把里面到期的事件 fire 掉。这是一个 \(O(1)\) 入队、\(O(1)\) 每 tick 检索的高效调度结构，远比「一个大优先队列」快，非常适合「事件密度高、延迟分布相对局部」的周期精确仿真。

但光有时间轮不够。有一种事件必须**本周期内立刻**发生，不能等到下一周期——那就是 `delay=0` 的「组合直达」。比如一个 converter channel 把请求转成另一种类型，这种转换逻辑上不该占一个时钟周期。为此框架引入了 **delta 事件（delta cycle）**：本周期内用一个小循环反复扫 `imm_events_`，直到所有零延迟事件都消化完（delta 表示「同一时钟周期内的第几个零时间步」）。

> 时间轮与 delta 的分工：
> \[ \text{事件} \in \begin{cases} \text{imm\_events\_} & \text{若 } delay = 0 \text{（同周期 delta 循环）} \\ \text{reg\_events\_}[fire\_cycle \bmod 4096] & \text{若 } delay > 0 \text{（时间轮）} \end{cases} \]

#### 4.3.2 核心流程

`SimPlatform::tick()` 推进一周期的内部顺序（这是理解时序的命根子）：

```text
SimPlatform::tick():
  1. fire_immediate_events()          // 先消化本周期已有的 delta 事件
  2. for (object : active_tick_):
        object->do_tick()             // 按对象创建顺序逐个 tick
        fire_immediate_events()       // 每个 tick 后再消化 delta（因为
                                      //   tick 里可能产生新的 delay=0 事件）
  3. ++cycles_                        // 全局周期 +1
  4. bucket = reg_events_[cycles_ & 4095]   // 看当前周期对应的时间轮槽
     for evt in bucket:
        if evt.cycles() <= cycles_:
            evt.fire()                // 送达延迟包 / 触发延迟回调
            删除 evt
        else:
            跳过（属于未来某圈的碰撞，下圈再看）
```

这个顺序带来两条关键时序规律（simobject.md §1 总结）：

- **创建顺序即 tick 顺序**：对象按 `create_object` 的调用顺序被 tick。一个模块在本周期 tick 时，读到的是「截至本周期的输入」；它产出的输出，对下游可见要等到**下一周期**（因为输出走 delay=1 的 channel，事件在下一周期的第 4 步才 fire）。
- **delta 是零时间**：`delay=0` 的事件在**同一个周期内**、在两次 tick 之间 fire。专用于组合扇出（converter、bypass），默认 `delay=1` 才是「寄存过的」正常流动。

谁来调 `tick()`？看真实入口：

```text
main.cpp 主循环（精简）：
  while (true):
    if (!dm.hart_is_halted()):
        SimPlatform::instance().tick()   // 每迭代推一个周期
    ...
```

#### 4.3.3 源码精读

**单例 `instance()`**——进程内唯一的平台：

[sim/common/simobject.h:L168-L176](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L168-L176) —— 用 Meyers 单例（函数内 `static`），`cycles()` 返回当前全局周期。

**时间轮配置与成员**——4096 槽 + 两层事件表：

[sim/common/simobject.h:L193-L218](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L193-L218) —— `WHEEL_SIZE=4096`、`WHEEL_MASK=4095`；`reg_events_` 是 4096 个侵入式链表（时间轮），`imm_events_` 是 delta 事件链表；`active_tick_`/`active_reset_` 是「只含重写了钩子的对象」的热路径子集。

**`schedule`：delay 0 vs delay>0 的分流**——channel 送达事件的排程：

[sim/common/simobject.h:L529-L540](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L529-L540) —— `delay==0` → 包封进 `SimChannelEvent`、带上当前 `delta_` 序号、塞进 `imm_events_` 并 `++delta_`；`delay>0` → 算出 `fire_cycle = cycles_ + delay`，按 `fire_cycle & WHEEL_MASK` 入时间轮对应槽。

**通用回调事件 `schedule(func, pkt, delay)`**——不依赖 channel 的延迟工作：

[sim/common/simobject.h:L554-L565](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L554-L565) —— 用 `SimCallEvent` 携带一个 `SmallFunction`（48 字节内小对象优化），fire 时 `func(pkt)`。用于周期性计数器滚动、延迟唤醒等「不走 channel」的工作。

**`tick()` 主循环**——上面流程图的真身：

[sim/common/simobject.h:L596-L620](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L596-L620) —— 第 598 行先消化 delta；第 600–603 行对 `active_tick_` 逐对象 tick，每个 tick 后再消化 delta；第 604 行 `++cycles_`；第 607–619 行处理当前周期时间轮槽里到期的事件（注意第 616 行：若事件 `cycles() > cycles_`，说明是「未来某圈落进同一槽」的碰撞，跳过留到以后）。

**`fire_immediate_events`：delta 循环**——本周期内反复扫零延迟事件：

[sim/common/simobject.h:L646-L660](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L646-L660) —— 外层按 `delta_` 的次数循环，每轮把序号匹配的 `imm_events_` 项 fire 掉；最后 `delta_ = 0`。这让一条组合链（A 的 delay=0 触发 B 的 delay=0）能在同一周期内逐级消化完。

**`reset()`**——清空所有事件、叫醒 active_reset 对象、周期归零：

[sim/common/simobject.h:L567-L594](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L567-L594) —— 先排干 `reg_events_` 和 `imm_events_`（第 569–584 行），再对 `active_reset_` 调 `do_reset()`，最后 `cycles_=0`、`delta_=0`。注意 `inflight_count()` **不**被 reset 重置（文档明确提醒：测试若依赖它需外部清零）。

**事件基类与两类事件**——`SimEventBase`、`SimChannelEvent`、`SimCallEvent`：

[sim/common/simobject.h:L107-L162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L107-L162) —— 三者都用对象池分配器（`PoolAllocator`，第 138/161 行）避免频繁 `new/delete`；`SimChannelEvent::fire()` 在 L662–L665 调 `channel_->receive_packet(pkt_)`，把「事件触发」与「channel 送达」对接起来。

**真实驱动**——`main.cpp` 的主循环反复调 `tick()`：

[sim/simx/main.cpp:L195-L213](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L195-L213) —— 第 199 行 `SimPlatform::instance().tick()` 每迭代推一个周期，被调试模块的 halt 状态门控；第 209–212 行用 `!processor.any_running()` 检测程序自然结束（没有 cluster 在跑）。

#### 4.3.4 代码实践

**实践目标**：追踪一个 `delay=1` 的包，看清它如何「在时间轮里待一周期、下周期被 fire」。

**操作步骤**：

1. 在 [sim/common/simobject.h:L529-L540](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L529-L540) 确认：一个 `send(pkt, 1)` 在周期 `c` 发生时，`fire_cycle = c + 1`，事件被挂进 `reg_events_[(c+1) & 4095]` 这个槽。
2. 在 [sim/common/simobject.h:L596-L620](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h#L596-L620) 确认：等 `tick()` 把 `cycles_` 推到 `c+1`（第 604 行），第 607 行才取出 `reg_events_[(c+1) & 4095]` 这个槽、fire 里面的事件（第 612 行），触发 `receive_packet`。
3. 在 [sim/simx/main.cpp:L195-L213](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L195-L213) 确认这个 `tick()` 是被主循环每个迭代调一次的。

**需要观察的现象**：「包在周期 c 被发送」与「包在周期 c+1 被对端看到」之间，框架做了「排程入轮 → 推进一周期 → 出轮 fire」三步，模块代码完全不参与这中间的等待。

**预期结果**：能用周期数精确描述一条 `delay=1` 的数据通路：发送周期 `c` → 到达周期 `c+1`。串 k 级 delay=1 的 channel，就是 k 周期的流水线深度。

> 待本地验证：在 `SimPlatform::schedule` 的 `delay>0` 分支加一行临时日志（如 `fprintf(stderr, "schedule fire=%lu now=%lu\n", fire_cycle, cycles_)`），跑一个 demo，观察同一包的 fire 周期与发送周期的差正好等于 delay。

#### 4.3.5 小练习与答案

**练习 1**：时间轮用 `reg_events_[fire_cycle & 4095]` 取模入槽，那「延迟超过 4096 周期」或「不同圈落到同一槽」怎么办？

> **答案**：时间轮用取模定位槽，所以延迟可超过 4096（事件带的 `cycles_` 字段是绝对周期号，不是槽号）。当两个不同周期的事件落到同一槽（例如周期 5 和周期 4101，都对应槽 5），fire 时在第 611 行用 `if (evt->cycles() <= cycles_)` 判断：只有真正到期的才 fire，没到期的（`evt->cycles() > cycles_`，第 616 行）跳过、留在槽里等下一圈。所以碰撞是正确处理的，代价是极少的「跳过」开销。

**练习 2**：`delay=0`（delta 事件）为什么不能直接在 `send` 时同步执行 `receive_packet`，而要排进 `imm_events_` 用 delta 循环处理？

> **答案**：为了保持「同周期内、对象 tick 之间」的事件顺序与可重入安全。如果 `delay=0` 立刻同步执行，那么一个对象的 `on_tick` 里 `send(pkt, 0)` 会立即触发下游的 `receive_packet`，等于在 tick 循环中途插入了对其他对象的副作用，破坏了「按创建顺序 tick」「一个 tick 产出的输出对下游下周期可见」这些时序不变量。排进 `imm_events_` 并用 `fire_immediate_events` 在 tick 之间统一消化，让 delta 事件在受控的边界上发生，组合链也能按 delta 序号逐级推进而不递归爆栈。

**练习 3**：`SimPlatform` 持有所有对象的 `shared_ptr`，那模块之间互相引用为什么文档建议用裸指针或 `weak_ptr`？

> **答案**：避免引用环（reference cycle）。若模块 A 持有 B 的 `shared_ptr`、B 又持有 A 的 `shared_ptr`，两者的引用计数永不为 0，即使 `SimPlatform::cleanup()` 清空了自己的 `objects_` 向量，A、B 仍互相持有、无法释放，造成内存泄漏。用裸指针（表示「我引用你但不拥有你」）或 `weak_ptr` 就打破了这个环。channel 的 bind 指针也是裸指针，同理。

---

### 4.4 基数规则：模块只通过 channel 通信

#### 4.4.1 概念说明

这是 simobject.md 开篇就强调的、**不可逾越**的一条铁律：

> **模块之间只能通过 channel 通信。** 一个 `SimObject` 观察或改变另一个模块的状态，**只能**通过它绑定的 channel 端口（如 `MemReq`/`MemRsp`）。它**绝不**能跨所有权层级直接抓别的对象。

反例（错误写法）：一个叶子单元爬上 `core->processor()->memsim()` 去直接读写全局 Memory 的 DRAM 后端，绕过了被建模的缓存路径。

正例（正确写法）：单元只往自己的输出 channel `try_send` 一个 `MemReq`，让请求像真实连线一样流经 coalescer / cache / NoC。

为什么这条规则「没得商量」？文档给了三条理由：

1. **channel 就是连线**。channel 图就是 SimX 对芯片真实连通性的建模。绕过 channel 等于建模了真实硬件里不存在的连线。
2. **保住时序/功能保真度与 SimX↔RTL 一致性**。一个走后门直接读 DRAM 的单元，可能读到「在真实硅片上还在缓存层级里飞行」的值——产生 RTL 永远不会产生的 SimX-only 结果。走 channel 路径才能让时序模型与功能效果一致，这正是 SimX 能当 RTL 预言机的前提。
3. **层级是所有权，不是调用图**。`Core` 拥有它的单元、`Processor` 拥有 `Memory`，这种父→子所有权是为生命周期/构造服务的，不能被「向上爬」（`child->parent()->…`）或「横向串」去调兄弟的内部。

本讲把它单列一个最小模块，是因为它**不是某个类的 API，而是整个 SimX 的建模哲学**，贯穿后面所有单元。

#### 4.4.2 核心流程

把基数规则翻译成「建模决策流程」：

```text
当模块 X 想读到/影响模块 Y 的某个状态时：
  ❌ 错误：X 持有 Y 的指针，直接调 Y->read()/write()
  ❌ 错误：X 向上爬 parent()->...->Y()
  ✅ 正确：X 在自己的某个 output channel 上 send 一个请求包
           → 包沿 bind 链流经中间模块（arbiter/cache/NoC…）
           → 最终到达 Y 的 input channel
           → Y 在自己的 on_tick 里 peek/pop 并处理
           → 响应沿另一条 channel 链回到 X
```

这条规则之所以和前三个基元强绑定：它正是建立在「channel = 连线 + 时序」之上的。如果 channel 不带延迟、不带背压，直接调函数也能跑通——但那样就丢了时序模型，SimX 就不再是周期精确的。

#### 4.4.3 源码精读

基数规则的权威表述与正反例在文档里：

[docs/simobject.md:L17-L51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md#L17-L51) —— 第 19 行的总纲「Modules communicate *only* through channels」；第 26–33 行的 WRONG/RIGHT 代码对比；第 36–50 行的三条理由（channel 是连线、保住 parity、层级是所有权）。

在代码层面，这条规则**不是用类型系统强制的**（没有任何 `private` 阻止你持有别人的指针），而是靠**纪律 + code review + model_parity CI 门控**来守。一旦你走了后门，SimX 与 RTL 的退休指令/周期就会对不上，model_parity 测试会失败——这是「违规的物理反馈」。

与基数规则配套的一个工具是 `tx_callback`（见 4.2.3 的 `receive_packet`）：当你只是想「监听一条已有 channel 的流量并做点反应」（计数、打日志、触发别处的副作用），不必为了监听而插入一个新的 SimObject，直接在 channel 上挂 `tx_callback` 即可——这避免了「为了监听而增加一个每周期 tick 的模块」。

#### 4.4.4 代码实践

**实践目标**：在真实代码里识别「走 channel」与「走后门」两种写法。

**操作步骤**：

1. 阅读 [docs/simobject.md:L17-L51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md#L17-L51) 的基数规则段落，把三条理由各用一句中文写下来。
2. 在仓库里挑一个 SimX 单元（如 `sim/simx/lsu_unit.cpp` 或 `sim/simx/alu_unit.cpp`），用 `grep` 搜索它如何把结果送出：应当看到的是形如 `Outputs[b].send(...)` 或 `out_req.try_send(...)` 的 channel 操作，**而不是**直接调 `core_->...()->write(...)` 之类后门。

**需要观察的现象**：一个合规的叶子单元，其 `on_tick` 里对「外界」的全部影响都体现为「往自己的 output channel send 包」；它读「外界」的全部来源都体现为「从自己的 input channel peek/pop」。

**预期结果**：能用一句话回答「为什么叶子单元不能直接读写全局 Memory」——因为它必须让请求经过缓存层级，才能保证 SimX 看到的时序和功能与 RTL 一致。

> 待本地验证：若尝试故意写一个走后门的单元，重新跑 model_parity（见 u7-l4 / u13-l4），预期会看到 simx 与 rtlsim 的退休指令或周期数对不上而测试失败——这就是基数规则的「物理反馈」。

#### 4.4.5 小练习与答案

**练习 1**：基数规则说「不能向上爬 parent()」，但构造时父对象确实会把 `this` 或子对象指针互相传。这矛盾吗？

> **答案**：不矛盾。所有权层级（parent 拥有 child）是为**构造与生命周期**服务的，构造期互相传指针建立拓扑是允许的。规则禁止的是在**仿真运行期（on_tick 里）**通过这些指针「向上爬」或「横向串」去直接调用别人的内部方法、绕过 channel。构造期接线（bind channel、存 raw pointer 备查）是合法的，运行期走后门读状态才违规。

**练习 2**：如果我只想统计一条 memory channel 上读写请求的比例，该新建一个「统计 SimObject」插进链路，还是用 `tx_callback`？

> **答案**：用 `tx_callback`。文档 §3/§5 明确：`tx_callback` 就是为「旁路监听」设计的，在送达周期看到包，不改变流向、不新增每周期 tick 的模块。新建一个 SimObject 插入链路会额外增加每周期开销、还可能改动时序，属于杀鸡用牛刀。只有当你需要**变换**包时才考虑插模块或用 converter bind。

---

## 5. 综合实践

**任务**：用一段话解释 **「channel 就是流水线」** 的含义，并说明一个流水线单元**为何不需要**内部 stage deque。

这是本讲规格指定的核心实践，要求把 4.1–4.4 串起来。请按以下步骤完成，并把结论写在笔记里：

1. **阅读** [sim/common/simobject.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/simobject.h) 与 [docs/simobject.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/simobject.md) §1–§2。
2. **回顾** 4.2.2 的包生命周期与 4.3.2 的 `tick()` 顺序。
3. **回答以下三个子问题**（写成一段连贯的话）：
   - 在 SimX 里，真实硬件的「级间寄存器（pipeline latch）」是由框架里的什么结构扮演的？（提示：带 `delay=1` 的 channel + 时间轮里的在途事件）
   - 一个三级流水线的功能单元，为什么不需要在 `on_tick` 里维护 `std::deque<Pkt> stage_[3]`？「这个包现在在第几级」这个状态被谁记住了？
   - 这套设计与「基数规则」有什么关系？（提示：正因为数据必须沿 channel 走、channel 自带延迟，时序才被正确建模，SimX 才能当 RTL 的预言机）

**参考答案要点**（写完后对照）：

> 「channel 就是流水线」指的是：SimX 用「带 `delay` 的 channel」来扮演真实硬件的级间寄存器——一个包 `send(pkt, 1)` 后，会被 `SimPlatform::schedule` 排进时间轮，过整整一个周期才在对端 `receive_packet` 里出现，这「一周期的等待」正好对应一级流水线寄存器。因此一个 N 级流水线单元不需要在内部写 `std::deque<Pkt> stage_[N]` 来记「每个包在第几级」——这个时间维度由框架的「在途事件（pending_count）+ 时间轮」替它记住了：包「在哪里」取决于它被排进了哪个 `fire_cycle` 的槽。模块的 `on_tick` 只需「从 input 取、算、往 output send」，每一级之间的「锁存」是 channel 的 delay。这也正是基数规则的根基：数据必须沿 channel 走，channel 的 delay 才能忠实地体现流水线时序，SimX 才能成为 RTL 的周期精确预言机。

**延伸（可选）**：在 `sim/simx/func_unit.h` 里看一个真实的多 block 功能单元（[sim/simx/func_unit.h:L38-L70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/func_unit.h#L38-L70)），确认它的 `Inputs`/`Outputs` 是 `SimChannel` 数组、本身不持有 stage deque——它是一个抽象基类，具体流水线由派生 ALU/FPU/LSU/SFU 各自的 `on_tick` 配合 channel 延迟来体现（详见 u6-l4）。

---

## 6. 本讲小结

- SimX 仿真框架的核心是 `simobject.h` 里的三个基元：`SimObject<Impl>`（可被时钟驱动的模块）、`SimChannel<Pkt>`（带延迟与背压的连线）、`SimPlatform`（拥有对象、推进时钟、排程事件的单例）。
- `SimObject` 用 CRTP 避免虚调用开销，并用成员指针检测实现「被动模块零开销」——没重写 `on_tick` 的模块不进每周期循环。
- `SimChannel` 有端点（自带 `RingQueue`）与转发（`bind` 到下游、自己不存）两种模式；`full()`/`size()` 沿 bind 链查询末端端点；背压靠 `occupancy = queue_size + pending_count` 在**发送时刻**强制。
- 时间维度由 `SimPlatform` 全权负责：`delay>0` 的事件进 4096 槽时间轮、`delay=0` 的事件进 delta 循环；`tick()` 的顺序（先 delta、再按创建序 tick、`++cycles_`、再处理时间轮槽）决定了「tick 顺序即创建顺序」「本 tick 产出下周期可见」。
- **「channel 就是流水线」**：带 `delay=1` 的 channel 扮演级间寄存器，所以流水线单元不需要内部 stage deque，「包在第几级」由框架的在途事件/时间轮记住。
- **基数规则**：模块只通过 channel 通信、绝不跨所有权层级走后门——这是 SimX 能当 RTL 周期精确预言机的根基，由 model_parity CI 门控兜底。

---

## 7. 下一步学习建议

本讲只搭了框架的地基（三个基元 + 一条铁律），还没接触任何真实的 GPU 模块。建议按依赖顺序继续：

- **u5-l2 SimX 入口与处理器层次**：看 `main.cpp` 如何用 `SimPlatform` 把 `processor→cluster→socket→core` 一层层 `create_object` 出来、如何往 DCR 写启动参数、主循环如何用 `any_running()` 判停。这将把本讲的「工厂 + tick 循环」落实到一个完整可执行程序上。
- **u5-l3 基数规则与模块分解**：把本讲 4.4 的基数规则展开成 SimX 与 RTL「逐模块对应」的体系，并认识 `types.h` 里 `MemReq`/`MemRsp` 等在 channel 上流动的载荷结构。
- 进阶之后（u6 系列）你会看到 `scheduler`/`decode`/`scoreboard`/`FuncUnit` 等真实模块如何用本讲的三个基元搭建出一条 6 级流水线——届时回看本讲，你会对「channel 就是流水线」有更具体的体会。

建议在进入 u5-l2 前，先确保自己能流畅回答本讲 4.2.5、4.3.5 的小练习，尤其是 `delay` 与时间轮的关系——它是后续所有时序讨论的基础。
