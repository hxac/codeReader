# mb_controller 主板控制器

## 1. 本讲目标

本讲聚焦 RFNoC 设备里**主板（motherboard）这一层**的控制中枢——`uhd::rfnoc::mb_controller`。读完本讲，你应当能够：

- 说清 `mb_controller` 在 `rfnoc_graph` 体系里的定位：它把「一块主板」上的时间、参考源、传感器、GPIO、EEPROM 等零散能力收口成一个统一接口。
- 理解**时间**在 RFNoC 设备里是怎么被追踪的：`timekeeper` 内部类、`time` 与 `ticks` 两种表示、以及 `tick_rate` 到 FPGA 周期寄存器（Q32）的换算。
- 掌握**时钟源 / 时间源 / 同步源**三类 API 的语义、它们的副作用，以及「为什么必须先选源、再设时、且期间不能流式」这条硬性顺序。
- 读懂 `synchronize()` 的同步算法：单 timekeeper 直接设、多 timekeeper 用 PPS 沿对齐、并用 10 ms 偏差阈值校验。
- 厘清 `rfnoc_graph::get_mb_controller()` 与 `rfnoc_graph::synchronize_devices()` 的关系，并理解为何 `rfnoc_graph` 构造期就会自动调用一次同步。

本讲承接 u3-l1（`rfnoc_graph` 会话）建立的「建图 → 取块 → 连接 → commit」骨架，把第 0 块主板控制器从黑盒打开。其中的时间同步思想（PPS、`set_time_next_pps`）在 u2-l8 已经用 `multi_usrp` 讲过，本讲是同一套机制在 RFNoC 层的对应物。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要有「主板控制器」这个角色？** 一台 USRP 不是一块铁板，它由主板（含 FPGA、时钟树、参考输入、网络/PCIe 接口）和插在上面的若干子板（daughterboard，承载射频前端）组成。u3-l1 讲的是「块网络」（Radio/DDC/FFT 这些 FPGA 块怎么连），那是**数据通路**层面的抽象；但还有一堆**非数据通路**的能力——「现在 FPGA 内部时间是多少」「时钟参考从哪个口进」「PPS 信号锁住了没有」「主板序列号是多少」——这些东西不属于任何单个块，却整块主板共用。`mb_controller` 就是把这些「主板级」能力打包在一起的控制器，**每块主板一个**。

**timekeeper 是什么？** RFNoC 设备的 FPGA 里有一个（少数设备有多个）专门数时钟周期的计数器，称为 timekeeper。它以某个固定频率（tick rate，通常等于主时钟率）不断累加，把「滴答数」换算成「时间」就是 FPGA 内部的绝对时间。这个时间是**定时命令（timed command）**的依据：你告诉某个块「在时间 T 执行某操作」，块就等到 timekeeper 数到 T 才动作。Radio 块也用它给采样打时间戳。

**时间同步为什么离不开 PPS？** 这一点 u2-l8 已建立：要把多块板子的时间对齐，光共享 10 MHz 参考只能让它们「走得一样快」（频率同步），但「现在几点」各板各数。必须靠一个**公共的秒脉冲（1 PPS）**作为对齐边沿，让所有板子在同一根脉冲上把时间寄存器写成同一个值。`mb_controller` 的 `synchronize()` 就是把这套「抓 PPS 沿 → 下一个 PPS 设时 → 校验」自动化。

> 名词速查：主板 mboard、子板 dboard、timekeeper（时基计数器）、tick rate（计数频率）、PPS（秒脉冲）、Q32（定点数表示）、timed command（定时命令）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `host/include/uhd/rfnoc/mb_controller.hpp` | 主板控制器与内嵌 `timekeeper` 类的**公共接口声明**，是本讲的主角。 |
| `host/lib/rfnoc/mb_controller.cpp` | `mb_controller` 的**非纯虚实现**：`synchronize()` 同步算法、`timekeeper` 的 time↔ticks 转换、`set_tick_rate` 的 Q32 换算、`register_timekeeper` 注册、GPIO/sync-updater 的默认（抛 `not_implemented_error`）实现。 |
| `host/include/uhd/rfnoc_graph.hpp` | `rfnoc_graph` 公共接口，其中 `get_mb_controller()` 与 `synchronize_devices()` 是 `mb_controller` 的对外入口。 |
| `host/lib/rfnoc/rfnoc_graph.cpp` | 上一条接口的实现：图如何持有主板控制器列表、构造期如何自动同步。 |
| `host/lib/include/uhdlib/usrp/common/mpmd_mb_controller.hpp` | 现代 N/X 系列设备的**具体子类**，用来展示「纯虚接口如何落地为 RPC 调用」。 |
| `host/lib/usrp/mpmd/mpmd_mb_controller.cpp` | 上一个子类的实现，展示构造期如何 `register_timekeeper`、timekeeper 如何转成对设备端 MPM 的 RPC。 |

> 阅读策略：先读 hpp 建立接口全貌，再读 cpp 看 `synchronize`/`timekeeper` 的算法本体，最后用 `mpmd_mb_controller` 对照「一个真实设备怎么实现这些纯虚函数」。

---

## 4. 核心概念与源码讲解

### 4.1 mb_controller 的定位与整体职责

#### 4.1.1 概念说明

`mb_controller` 是一个**抽象基类**，定义「一块主板」上所有与数据通路无关的控制能力。它的设计有三个要点：

1. **每块主板一个实例**。一个 `rfnoc_graph` 可能跨多块主板（多设备），图内部维护一个 `mb_controller` 列表，用主板下标 `mb_index` 寻址。
2. **非数据通路控制的总收口**。时间（timekeeper）、参考源（clock/time source）、主板传感器（`ref_locked` 等）、GPIO 驱动源、主板 EEPROM，全都挂在这里。
3. **接口与实现分离**。`mb_controller` 只声明纯虚函数（接口契约），具体行为由各设备族的子类实现——老设备走 `x300_mb_controller`（直接寄存器访问），现代 N/X 系列走 `mpmd_mb_controller`（转成对设备端 MPM 进程的 RPC）。

它继承自 `uhd::noncopyable`（不可拷贝，因为背后是真实硬件句柄）和 `discoverable_feature_getter_iface`（可被查询「这块主板支持哪些可选特性」，如参考时钟校准、触发 IO 模式等）。

#### 4.1.2 核心流程

从用户视角，拿到一个 `mb_controller` 的典型用法分四块：

```
rfnoc_graph::make(device_args)          // 建图（u3-l1）
        │
        ▼
graph->get_mb_controller(mb_index)      // 取第 mb_index 块主板控制器
        │
        ├── 时间：get_timekeeper(0)->get/set_time_now/next_pps
        ├── 参考源：set_sync_source("clock_source=...,time_source=...")
        ├── 查询：get_sync_sources() / get_sensor("ref_locked") / get_eeprom()
        └── 同步：mbc->synchronize(其它主板列表) 或 graph->synchronize_devices(...)
```

#### 4.1.3 源码精读

类的声明与继承关系，体现了「不可拷贝 + 可发现特性」两个设计约束：

[host/include/uhd/rfnoc/mb_controller.hpp:28-31](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L28-L31) —— `mb_controller` 继承 `noncopyable` 与 `discoverable_feature_getter_iface`，`UHD_API` 控制跨动态库导出。

接口被分成若干个语义段落，公共头里用注释横线明确分区（如「Motherboard Control」「Timebase API」），便于阅读。其中纯虚方法意味着**子类必须全部实现**，例如 `get_mboard_name()`、`set_time_source()`：

[host/include/uhd/rfnoc/mb_controller.hpp:227-270](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L227-L270) —— `get_mboard_name()` 与 `set_time_source()` 都是 `= 0` 的纯虚函数；后者文档还提示了「同值重复调用不会强制重新初始化硬件」这一实现自由度。

而 `synchronize()`、GPIO 相关方法、`register_sync_source_updater()` 在基类里有**默认实现**（非纯虚），子类可以覆盖也可以直接用基类版本。GPIO 的基类默认实现是直接抛「不支持」：

[host/lib/rfnoc/mb_controller.cpp:264-289](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L264-L289) —— `get_gpio_banks()` 默认返回空，`get_gpio_srcs/get_gpio_src/set_gpio_src` 默认抛 `not_implemented_error`，这样没有 GPIO 控制能力的设备无需重写也能编译，调用时按异常处理。

具体子类 `mpmd_mb_controller` 展示了「一个真实设备要 override 多少东西」：

[host/lib/include/uhdlib/usrp/common/mpmd_mb_controller.hpp:28-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/usrp/common/mpmd_mb_controller.hpp#L28-L29) —— 注释明说「每块主板一个」，它把一串 RPC 调用抽象成了 `mb_controller` 的标准接口。

[host/lib/include/uhdlib/usrp/common/mpmd_mb_controller.hpp:86-112](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/usrp/common/mpmd_mb_controller.hpp#L86-L112) —— 它把基类几乎每个纯虚函数都 override 了一遍，且自己重写了 `synchronize()`（见 4.4）。

#### 4.1.4 代码实践

**目标**：在源码层面确认「主板控制器是一份接口契约，由各设备族各自实现」。

**操作步骤**：

1. 打开 `host/include/uhd/rfnoc/mb_controller.hpp`，统计 `= 0`（纯虚）的方法数与有默认实现的方法数。
2. 在 `host/lib/` 下搜索 `: public mb_controller`（或 `public uhd::rfnoc::mb_controller`），列出所有具体子类。
3. 对比 `mpmd_mb_controller.hpp` 与 `x300_mb_controller.hpp`，看它们各自 override 了哪些方法。

**需要观察的现象**：会找到至少两个子类（`mpmd_mb_controller`、`x300_mb_controller`），它们覆盖的方法集合大体相同，但实现机制完全不同（RPC vs 寄存器）。

**预期结果**：纯虚方法对应「所有主板都必须有的能力」（如 `set_time_source`、`get_mboard_name`），带默认实现的方法对应「可选能力」（如 GPIO）。

> 无需硬件，纯源码阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mb_controller` 继承 `noncopyable`？

**参考答案**：因为一个 `mb_controller` 实例背后绑定了真实硬件资源（寄存器映射、RPC 连接、timekeeper 句柄），若允许拷贝会产生两个对象操作同一硬件、生命周期混乱；用 `shared_ptr`（`sptr`）共享单一实例才是正确做法。

**练习 2**：GPIO 相关方法为什么在基类里给出「抛异常」的默认实现，而不是设成纯虚？

**参考答案**：并非所有主板都有受 `mb_controller` 管理的 GPIO。设成纯虚会强迫每个子类都写一份空实现；给默认抛 `not_implemented_error` 的版本后，没有 GPIO 的设备直接继承默认行为即可，有 GPIO 的设备再 override——这是「接口与可选能力」的常见取舍。

---

### 4.2 timekeeper 时基模型

#### 4.2.1 概念说明

`timekeeper` 是 `mb_controller` 里**内嵌的类**（`mb_controller::timekeeper`），代表 FPGA 里那个数时钟周期的计数器。它有两种时间表示：

- **ticks**：裸的计数器值（`uint64_t`），是硬件寄存器里真实存放的东西。
- **time**（`uhd::time_spec_t`）：把 ticks 除以 tick rate 得到的「秒 + 小数秒」时间，是用户友好表示。

两者靠 tick rate 互相换算：`time = ticks / tick_rate`。`timekeeper` 把面向硬件的 `*_ticks_*` 方法设为纯虚（不同设备的寄存器读写方式不同），而面向用户的 `*_time_*` 方法在基类里用 tick rate 把它们翻译过去。

关键点：**改变时钟源会复位 timekeeper**（参考源切换会重打时钟树），所以设时间必须在选源之后、且不能在流式期间。这与 u2-l8 的结论一致，在 RFNoC 层由 `timekeeper` 文档再次强调。

#### 4.2.2 核心流程

timekeeper 的读/写有「立即」和「下一个 PPS」两种时机：

```
读：  get_ticks_now()        → 直接读寄存器（纯虚，子类实现）
      get_time_now()         = from_ticks(get_ticks_now(), tick_rate)   ← 基类翻译层
      get_time_last_pps()    = from_ticks(get_ticks_last_pps(), tick_rate)

写：  set_ticks_now(t)       → 立刻写寄存器（纯虚）
      set_time_now(time)     = set_ticks_now(time.to_ticks(tick_rate))  ← 基类翻译层
      set_ticks_next_pps(t)  → 等下一个 PPS 沿再写（纯虚，用于多板对齐）
      set_time_next_pps(time)= set_ticks_next_pps(time.to_ticks(tick_rate))

配置：set_tick_rate(rate)    → 记录 rate，并把 1/rate 换算成 Q32 周期写入 FPGA（set_period，纯虚）
```

tick rate 到 FPGA 周期寄存器用的是 **Q32 定点数**：一个 ns 周期对应 `1 << 32`。换算公式为

\[
\text{period\_ns} = \frac{10^9}{\text{tick\_rate}} \times 2^{32}
\]

这样 FPGA 用整数定点就能表示「每个 tick 等于多少纳秒」，避免浮点。

#### 4.2.3 源码精读

`timekeeper` 类整体声明，注意哪些是纯虚、哪些是基类已实现：

[host/include/uhd/rfnoc/mb_controller.hpp:62-192](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L62-L192) —— `get_ticks_now/get_ticks_last_pps/set_ticks_now/set_ticks_next_pps/set_period` 为纯虚（=0），面向用户的 `get_time_now/set_time_now/set_time_next_pps/get_time_last_pps` 非纯虚。

文档对「改时钟源会丢时间」的明确警告，正是 4.3 节顺序约束的依据：

[host/include/uhd/rfnoc/mb_controller.hpp:138-148](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L138-L148) —— `set_time_next_pps` 注释强调：切换 clock source 后此前设置的时间很可能丢失，应「先选源、后设时」。

基类的「time ↔ ticks 翻译层」，全部委托给 `time_spec_t::from_ticks / to_ticks`：

[host/lib/rfnoc/mb_controller.cpp:210-228](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L210-L228) —— `get_time_now()` 调 `get_ticks_now()` 再 `from_ticks(..., _tick_rate)`；`set_time_now()` 反向。这是典型的「模板方法」：算法骨架在基类，硬件访问延迟到子类。

tick rate 到 Q32 周期的换算实现：

[host/lib/rfnoc/mb_controller.cpp:230-242](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L230-L242) —— `_tick_rate` 变化时才更新；`period_ns = 1e9 / tick_rate * (1<<32)`，再交给纯虚 `set_period()` 写入硬件。若 tick rate 未变则直接 return，避免无谓的硬件写。

timekeeper 的注册机制——设备驱动在构造期把每个 timekeeper 实例登记进 `mb_controller`：

[host/lib/rfnoc/mb_controller.cpp:244-262](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L244-L262) —— `get_num_timekeepers()` 返回内部 map 大小；`get_timekeeper(idx)` 越界抛 `index_error`；`register_timekeeper` 是 `protected`，仅供子类构造时调用，把 timekeeper 存进 `_timekeepers` 这个 `unordered_map`。

具体看 `mpmd_mb_controller` 怎么注册并实现一个 timekeeper：

[host/lib/usrp/mpmd/mpmd_mb_controller.cpp:121-124](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_controller.cpp#L121-L124) —— 构造期先问设备端 MPM「你有几个 timekeeper」（`_rpc->get_num_timekeepers()`），再逐个 `register_timekeeper(tk_idx, make_shared<mpmd_timekeeper>(...))`。

[host/lib/usrp/mpmd/mpmd_mb_controller.cpp:160-173](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_controller.cpp#L160-L173) —— `mpmd_timekeeper` 把 4 个纯虚 ticks 方法翻译成对 MPM 的 RPC（如 `get_ticks_now` → `_rpc->get_timekeeper_time(_tk_idx, false)`）。这就是「time↔ticks 在基类翻译、ticks↔硬件在子类落地」的完整闭环。

#### 4.2.4 代码实践

**目标**：理解 time/ticks/tick_rate 三者的换算关系。

**操作步骤**（源码阅读 + 纸笔演算）：

1. 读 [mb_controller.cpp:210-228](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L210-L228)，确认 `get_time_now = from_ticks(get_ticks_now, tick_rate)`。
2. 假设某设备 tick rate = 200e6（200 MHz），当前 ticks = 200_000_000，手算 `time_now` 应为多少秒。
3. 若想把时间设成 1.5 s，写出 `set_time_now(time_spec_t(1.5))` 会传给 `set_ticks_now` 的整数值。

**需要观察的现象 / 预期结果**：

- `200_000_000 / 200e6 = 1.0 s`。
- `1.5 * 200e6 = 300_000_000` ticks。

> 若有硬件：可在 `multi_usrp` 或 `rfnoc_graph` 里 `mbc->get_timekeeper(0)->get_tick_rate()` 回读，确认其等于主时钟率；此步「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `timekeeper` 把 ticks 方法设成纯虚、time 方法在基类实现？

**参考答案**：因为「读写硬件计数器」是设备相关的（寄存器地址、RPC 接口各不同），必须由子类提供；而「time = ticks / tick_rate」的换算对所有设备都一样，放基类可消除重复代码。这是模板方法模式。

**练习 2**：`set_tick_rate` 里 `period_ns = 1e9 / tick_rate * (1<<32)`，当 tick rate = 1e9 时 period_ns 等于多少？含义是什么？

**参考答案**：`1e9/1e9 * (1<<32) = 1<<32`，即注释里说的「period == 1ns 意味着 period_ns == 1<<32」。表示每个 tick 正好 1 纳秒，定点数 Q32 下满量程代表 1 ns。

---

### 4.3 时钟与时间参考源 API

#### 4.3.1 概念说明

这部分接口管理「主板的频率参考和时间参考从哪来」：

- **clock source**：频率参考源，典型是 10 MHz 信号，决定所有板子「走得一样快」。常见值 `internal` / `external` / `gpsdo`。
- **time source**：时间参考源，典型是 1 PPS 秒脉冲，决定「现在几点对齐」。常见值同上。
- **sync source**：把 clock + time 打包成一次设置，部分设备比分别调用更快。

关键的两条**副作用纪律**（u2-l8 已讲，RFNoC 层同样适用）：

1. **改 clock/time source 会重打 FPGA 时钟树，进而复位 timekeeper**——因此「先选源、再设时」，期间不能流式。
2. **某些设备只允许特定 clock/time 组合**，改一个可能连带改另一个，所以「读回当前值是唯一确定现状的办法」。

#### 4.3.2 核心流程

参考源的设置与查询分两组：

```
设置： set_clock_source(src)         set_time_source(src)
       set_sync_source(clock, time)  ← 两字符串简写
       set_sync_source(device_addr)  ← "clock_source=...,time_source=..." 一次设俩
查询： get_clock_source() / get_time_source()          ← 读回当前值
       get_clock_sources() / get_time_sources()        ← 列出该设备支持的可选值
       get_sync_source() / get_sync_sources()          ← 组合查询
输出： set_clock_source_out(bool) / set_time_source_out(bool)  ← 是否把参考转发到输出接口
```

#### 4.3.3 源码精读

`set_time_source` / `set_clock_source` 的纯虚声明与详尽文档（含同值不重初始化的语义）：

[host/include/uhd/rfnoc/mb_controller.hpp:284-337](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L284-L337) —— 文档强调「reading back is the only certain way」以及重复设同值不会强制硬件重配。

`set_sync_source` 有两个重载，两字符串版本是 `device_addr_t` 版本的简写：

[host/include/uhd/rfnoc/mb_controller.hpp:339-380](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L339-L380) —— 注意带代码示例（`graph->get_mb_controller(0)->set_sync_source(device_addr_t("clock_source=external,time_source=external"))`），以及「重配 sync source 会影响 FPGA 时钟与 timekeeping，强烈建议最先配置、设时在此之后、期间不可流式」的强约束。

参考输出与传感器查询：

[host/include/uhd/rfnoc/mb_controller.hpp:394-427](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/mb_controller.hpp#L394-L427) —— `set_clock_source_out` / `set_time_source_out` 默认启用输出便于级联；`get_sensor(name)` 读主板级传感器（如 `ref_locked` 频率锁、GPSDO 状态），这正是 u2-l8 里判断「参考是否锁住」的入口在 RFNoC 层的位置。

#### 4.3.4 代码实践

**目标**：编写一段「先选源、再确认可选值、读回当前值」的最小序列，体会顺序纪律。

**操作步骤**：

1. 阅读上面的 permalink，确认 `get_sync_sources()` 返回的是「该设备支持的全部组合」。
2. 阅读示例代码（hpp 第 358-362 行）。
3. 写出如下伪代码（**示例代码**，非项目原文件）：

```cpp
// 示例代码：参考源设置的标准顺序
auto mbc = graph->get_mb_controller(0);

// (1) 先看支持哪些组合
for (const auto& ss : mbc->get_sync_sources()) {
    std::cout << ss.to_pp_string() << std::endl;
}
// (2) 一次性设 clock + time
mbc->set_sync_source(
    uhd::device_addr_t("clock_source=external,time_source=external"));
// (3) 读回确认（唯一可靠的现状来源）
std::cout << "clock=" << mbc->get_clock_source()
          << " time=" << mbc->get_time_source() << std::endl;
// (4) 确认频率参考已锁（传感器）
std::cout << "ref_locked=" << mbc->get_sensor("ref_locked").to_pp_string()
          << std::endl;
```

**需要观察的现象 / 预期结果**：`get_sync_sources()` 列出的组合随设备不同（X4x0 与 B2x0 支持项不同）；读回值可能与期望不同——这正说明「读回是唯一确定现状的办法」。

> 无硬件时：阅读 `mpmd_mb_controller.cpp` 里 `get_sync_sources` / `set_sync_source` 的实现，确认它们都是转成对 MPM 的 RPC。此步「待本地验证」运行输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `set_sync_source` 的文档反复强调「要在做其他事之前配置，且设时间要排在其后」？

**参考答案**：因为切换参考源会重打 FPGA 时钟树、复位 timekeeper。如果先设时再切源，时间会被清掉；如果在流式期间切源，正在传输的样本会失步。正确顺序是「选源 → 等锁 → 设时 → 开始流式」。

**练习 2**：`set_clock_source("external")` 调用后，`get_time_source()` 的值一定不变吗？

**参考答案**：不一定。文档明说某些设备只支持特定 clock/time 组合，改 clock source 可能连带改 time source。所以「读回当前值」是唯一确定现状的办法，不能假设未显式设置的项保持不变。

---

### 4.4 synchronize 多主板同步算法

#### 4.4.1 概念说明

`synchronize()` 是 `mb_controller` 里逻辑最复杂的方法，目标是把一组主板的 timekeeper 对齐到同一时间。它的算法分两条路径：

- **单 timekeeper**：直接 `set_time_now`，无需 PPS。
- **多 timekeeper**（多板，或单板多 timekeeper）：必须借助 PPS 沿对齐——先抓一次 PPS 沿，再让所有 timekeeper 在「下一个 PPS」把时间设成同一个值，最后用 10 ms 偏差阈值校验。

注意一个前提：只有当时间源是**可同步源**（`gpsdo` 或 `external`）时，多板才能真正对齐；若是 `internal`，算法会退化为「板内 timekeeper 之间对齐、板间不对齐」，并打告警。

#### 4.4.2 核心流程

`synchronize()` 主流程（`mb_controller.cpp`）：

```
mb_controller::synchronize(mb_controllers, time_spec, quiet):
  1. 校验所有主板共享同一个 time_source；
     若该 source 不可同步(gpsdo/external 之外) 且板数>1 → 告警
  2. 若 source 可同步：
     收集所有主板的所有 timekeeper → 一把交给 sync_tks()
  3. 否则(internal)：
     逐板收集各自 timekeeper，用 std::async 并行对每板调 sync_tks()
     （板内对齐，板间不对齐）
```

`sync_tks()`（同步核心，匿名命名空间）：

```
sync_tks(timekeepers, time_spec, quiet):
  Case 1: 只有 1 个 timekeeper
    → set_time_now(time_spec) 直接设，返回 true
  Case 2: 多个 timekeeper
    1) 抓 PPS 沿：循环读 get_time_last_pps()，等它跳变（超时 1100ms 抛异常）
    2) 对每个 timekeeper 调 set_time_next_pps(time_spec)
    3) sleep 1s 等下一个 PPS 生效
    4) 校验：每个 tk 的 get_time_now() 与首 tk 的差 ≤ 10ms（MAX_DEVIATION）
       超差 → 告警并返回 false
    返回 true
```

10 ms 这个阈值是刻意选的：它**大于一次往返（RTT）但又足够小**——既能容忍网络/PCIe 读取延迟带来的正常差异，又能抓住真正失步（差 1 整秒）的情况。

#### 4.4.3 源码精读

可同步参考源的白名单（只有这两个源能跨板对齐）：

[host/lib/rfnoc/mb_controller.cpp:21-22](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L21-L22) —— `SYNCHRONIZABLE_REF_SOURCES = {"gpsdo", "external"}`。

`mb_controller::synchronize` 主体——先校验共享同一时间源，再按「可同步 / 不可同步」分两条路径：

[host/lib/rfnoc/mb_controller.cpp:124-165](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L124-L165) —— 校验所有板 `get_time_source()` 相同；可同步时把「所有板 × 所有 timekeeper」拍平成一张表，一次性 `sync_tks`。

[host/lib/rfnoc/mb_controller.cpp:167-200](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L167-L200) —— `internal` 路径用 `std::launch::async` **并行**对每块板分别 `sync_tks`（板内对齐最多耗时 2 秒/板，并行避免串行累加），最后用 `std::all_of` 汇总。

`sync_tks` 的两种 case——单 timekeeper 直接设，多 timekeeper 用 PPS：

[host/lib/rfnoc/mb_controller.cpp:54-89](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L54-L89) —— Case 1 单 tk 直接 `set_time_now`；Case 2 先 `while (get_time_last_pps() 未跳变) sleep 1ms` 抓沿（超时 1100ms 抛「No PPS detected」），再对每个 tk `set_time_next_pps(time_spec)`，然后 `sleep 1s`。

10 ms 偏差校验——读回所有 tk 与首 tk 比较：

[host/lib/rfnoc/mb_controller.cpp:91-119](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L91-L119) —— `MAX_DEVIATION = 0.01`（10 ms），若 `time_i - time_0 > 10ms` 或倒退则告警返回 false。

#### 4.4.4 代码实践

**目标**：跟踪一次「抓 PPS 沿 → 下一个 PPS 设时 → 校验」的完整调用链，理解超时与偏差阈值。

**操作步骤**：

1. 打开 [mb_controller.cpp:54-119](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L54-L119)。
2. 标出三处关键常数：`1100ms`（抓 PPS 超时）、`1s`（等下一个 PPS 生效）、`0.01`（10 ms 偏差阈值），各说明含义。
3. 思考：若两块板不在同一根 PPS 上，会在哪一步失败、返回什么？

**需要观察的现象 / 预期结果**：

- 抓沿超时 → 抛 `runtime_error("... may not be getting a PPS signal!")`（[第 73-77 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L73-L77)）。
- 设时后读回偏差超 10 ms（典型是差了整 1 秒）→ 打 WARNING 并返回 `false`（[第 101-115 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/mb_controller.cpp#L101-L115)）。

> 无需硬件，纯源码跟踪。若有硬件并接入公共 PPS/10 MHz，可在日志里看到 `MB_CTRL` 的 "Synchronizing N timekeepers" 与可能的 deviation 告警。运行输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么抓 PPS 沿要先读一次 `get_time_last_pps()`，再循环等它变化？

**参考答案**：为了对齐到「下一个」PPS 边沿。先记下当前 last_pps 值，循环轮询直到它跳变——跳变那一刻就是刚过去的一个 PPS 沿，于是「下一个 PPS」就是一个确定的、约 1 秒后的未来时刻，所有板都能在这同一个未来沿上 `set_time_next_pps`，从而避免各板因网络延迟落在不同 PPS 沿两侧而差 1 秒。

**练习 2**：`internal` 时间源时，`synchronize()` 为什么用 `std::async` 并行？

**参考答案**：`internal` 意味着各板没有公共参考、板间无法对齐，但**板内**若有多个 timekeeper 仍可对齐（`sync_tks` 的 Case 2 最多耗时约 2 秒）。串行处理多块板会累加（N 板 × 2 秒），并行后墙钟时间≈单板时间，显著加快初始化。

---

## 5. 综合实践

把本讲四个模块串成一个最小任务：**通过 `mb_controller` 写出「获取当前时间 → 设置参考源 → 用 PPS 触发同步 → 回读确认」的完整调用序列**。

**示例代码**（非项目原文件，用于演示 API 组合）：

```cpp
// 示例代码：mb_controller 时间获取/设置 + 触发同步
#include <uhd/rfnoc_graph.hpp>
#include <uhd/types/time_spec.hpp>
#include <iostream>

int main()
{
    // (a) 建图并取第 0 块主板控制器
    auto graph   = uhd::rfnoc::rfnoc_graph::make("type=x4xx,addr=...");
    auto mbc     = graph->get_mb_controller(0);

    // (b) 读当前时间（timekeeper 0）
    auto tk      = mbc->get_timekeeper(0);
    std::cout << "time_now = " << tk->get_time_now().get_real_secs() << " s\n";
    std::cout << "tick_rate = " << tk->get_tick_rate() << " Hz\n";

    // (c) 选定参考源：必须在设时之前，且不可在流式期间
    mbc->set_sync_source(uhd::device_addr_t(
        "clock_source=external,time_source=external"));

    // (d) 触发同步：把本会话内所有主板对齐到 time_spec
    //     内部走 mb_controller[0]->synchronize(全部主板列表, ...)
    bool ok = graph->synchronize_devices(uhd::time_spec_t(0.0), false);
    std::cout << "synchronize_devices ok = " << std::boolalpha << ok << "\n";

    // (e) 回读确认
    std::cout << "time_now after sync = "
              << tk->get_time_now().get_real_secs() << " s\n";
    return 0;
}
```

**配套源码阅读任务**（无硬件也能完成）：

1. 打开 [rfnoc_graph.cpp:488-500](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L488-L500)，确认 `synchronize_devices` 实际是把图内全部主板控制器拷一份，交给 `mb_controller[0]->synchronize(...)`。
2. 打开 [rfnoc_graph.cpp:107-109](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L107-L109)，确认 `rfnoc_graph` **构造期**就会以 `time_spec_t(0.0)`、`quiet=true` 自动调一次 `synchronize_devices`——这正是 rfnoc_graph.hpp 注释里「初始化时总是被调用」的来源。
3. 画出本例 (a)-(e) 步骤与底层 `synchronize` → `sync_tks`（Case 1 还是 Case 2？取决于板数）的对应关系。

**预期结果**：单板单 timekeeper 时走 `sync_tks` Case 1（直接 `set_time_now(0.0)`）；多板共享 external 参考时走 Case 2（PPS 对齐）。`quiet=false` 时失败会在日志看到 `MB_CTRL` 的 WARNING。

> 实际运行需真实 USRP 硬件与参考源接线，运行输出「待本地验证」。

## 6. 本讲小结

- `mb_controller` 是「一块主板」上**非数据通路**控制能力（时间、参考源、传感器、GPIO、EEPROM）的总收口，**每块主板一个实例**，是抽象基类，由 `mpmd_mb_controller` / `x300_mb_controller` 等子类落地。
- **时间**由内嵌的 `timekeeper` 类承载：硬件相关方法（`*_ticks_*`、`set_period`）纯虚，面向用户的 `*_time_*` 在基类用 tick rate 翻译；构造期由子类 `register_timekeeper` 登记。
- **tick rate → FPGA 周期**用 Q32 定点换算：`period_ns = 1e9/tick_rate * (1<<32)`，让硬件用整数表示「每 tick 多少纳秒」。
- **参考源 API**（`set_clock/time/sync_source` + 查询）有两条硬纪律：改源会复位 timekeeper（先选源、后设时、期间不流式）；某些设备 clock/time 联动，**读回是唯一确定现状的办法**。
- **`synchronize()`** 分两路：可同步源（`gpsdo`/`external`）时把所有 timekeeper 一次对齐；`internal` 时板内并行对齐。核心算法 `sync_tks` 用 PPS 沿对齐，并以 **10 ms 偏差阈值**校验。
- `rfnoc_graph::get_mb_controller()` 取控制器，`synchronize_devices()` 委托给 `mb_controller[0]->synchronize(全部主板)`；图**构造期自动以 0.0、quiet=true 同步一次**。

## 7. 下一步学习建议

- **u3-l5 属性传播与 experts 框架**：`mb_controller` 的参考源、tick rate 变更会触发属性树/experts 的传播（采样率 → DSP 缩放等），建议接着学 experts 依赖图。
- **u3-l6 常用 RFNoC 块**：`radio_control` 提供 `get_time_now()` / `get_ticks_now()`，可直接从 Radio 块读时间，比经 timekeeper 延迟更低（见本讲 timekeeper 文档注释）——可对照阅读 `host/lib/rfnoc/radio_control_impl.cpp`。
- **u4-l4 MPMD 设备实现**：本讲多次引用 `mpmd_mb_controller` 把接口翻译成 RPC，其对面就是设备端的 MPM 进程，建议深入 `host/lib/usrp/mpmd/` 理解主机↔设备的通信边界。
- 如需在 `multi_usrp` 层复用同样能力（老 API），可回看 u2-l8 的 `set_time_unknown_pps` 与 `get_mboard_sensor`，它们与本讲的 `synchronize` / `get_sensor` 是同一套底层机制的两层封装。
