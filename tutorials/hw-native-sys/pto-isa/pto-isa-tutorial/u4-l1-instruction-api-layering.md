# 指令 API 分层：从公共接口到 TF 层

> 本讲为 update 版本。相对于上一版讲义基线（HEAD `0dbecbe`），本讲引用的源码有两处关键变化：一是 `pto_instr_impl.hpp` 中 `__DAV_VEC__` 保护的 `TCvt.hpp` 补上了缺失的 `#endif`（修复了条件块"吞噬"后续全部 A5 头的装配事故）；二是新增了 `PTO_NPU_ARCH_A6` 条件块，恢复了 A6 架构指令头的接入。本讲按当前 HEAD（`be5ccb7`）完整重写。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `pto_instr.hpp` 中任何一条指令的公共签名：`dst/src` 操作数在前、`WaitEvents&... events` 变参在尾、返回 `RecordEvent`。
2. 理解「接口声明在 common、实现按后端归位」的分层设计：公共 API 经 `MAP_INSTR_IMPL` 宏转发到 `*_IMPL`，再由 `pto_instr_impl.hpp` 按宏装配出 CPU / a2a3 / a5 / a6 / kirin 各套实现。
3. 能按命名规则推断一条陌生指令的含义：`S` 后缀（标量）、`C` 变体（立即数）、`_ACC`（累加）、`_MX`（微缩放格式）、`_ASYNC`（异步）等。
4. 说清 `__DAV_VEC__` 条件保护为什么必须立即闭合、`PTO_NPU_ARCH_A6` 条件块在装配流中的位置，从而建立「User API → IMPL → TF → CCE 内建」的完整抽象层次认知。

## 2. 前置知识

本讲默认你已学完以下内容（不再重复展开）：

- **u1-l5（统一入口）**：`include/pto/pto-inst.hpp` 按 `__CPU_SIM` / `__CCE_AICORE__` / `__COSTMODEL` 三个宏选择后端，`__NPU_ARCH__` 经 `arch_macro.hpp` 翻译成 `PTO_NPU_ARCH_A2A3 / A5 / A6 / KIRIN*` 等内部宏。本讲深入它包含的两层：公共声明层 `pto_instr.hpp` 与装配层 `pto_instr_impl.hpp`。
- **u3-l1（事件模型）**：跨流水线数据依赖用 `set_flag`/`wait_flag` 表达；`RecordEvent` 是「指令已完成、可被等待」这一事实的类型化写法。本讲从**指令声明层**再看这两个概念如何成为统一签名的一部分。
- **u2-l1（类型系统）**：`PTO_INST` / `PTO_INTERNAL` / `AICORE` 等宏让同一份声明在 CCE 编译器与主机编译器下都合法。本讲只引用其定义，聚焦「公共 API 与内部实现的可见性差异」。
- **u1-l4（tadd 用例）**：你已经以使用者身份调用过 `TASSIGN`/`TLOAD`/`TADD`/`TSTORE`。本讲回答的问题是：这些调用点背后的函数签名是**谁**、以**什么规则**声明和分发的。

一个贯穿本讲的直觉：**PTO 把"指令长什么样"和"指令在具体芯片上怎么做"彻底拆开**。前者全仓库只写一遍（common），后者每个后端各写一遍（cpu / npu/a2a3 / npu/a5 / npu/a6 …），中间靠宏拼接缝合。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `include/pto/common/pto_instr.hpp` | 全部指令的公共 API 声明（约 2560 行） | 主战场：统一签名约定、`MAP_INSTR_IMPL` 宏族 |
| `include/pto/common/pto_instr_impl.hpp` | 装配枢纽：按架构宏批量 include 各后端 `*_IMPL` 头 | 本讲重点变化所在（`__DAV_VEC__` 修复、A6 接入） |
| `include/pto/common/type.hpp` | `PTO_INST` / `PTO_INTERNAL` 宏定义 | 解释公共/内部可见性 |
| `include/pto/common/event.hpp` | `RecordEvent`、`WaitAllEvents` 定义 | WaitEvents 变参的落点 |
| `include/pto/common/arch_macro.hpp` | `__NPU_ARCH__` → `PTO_NPU_ARCH_*` 翻译；CPU sim 下补 DAV 宏 | 架构头文件组织的入口 |
| `include/pto/npu/a6/header.hpp` | A6 汇总头：专属指令 + 复用 A5 | A6 接入方式的物证 |
| `include/pto/npu/a2a3/TAdd.hpp` | a2a3 后端 `TADD_IMPL`（含 Check 与 TF 结构体） | 「IMPL → TF → CCE」链路样例 |
| `docs/isa/conventions.md` | ISA 文档共享约定 | 命名约定与操作数约束的文档侧依据 |

## 4. 核心概念与源码讲解

本讲的五个最小模块：**公共 API 声明**、**WaitEvents 变参**、**实现分发**、**指令命名约定**、**架构头文件组织**。

### 4.1 公共 API 声明：pto_instr.hpp 的统一签名

#### 4.1.1 概念说明

`pto_instr.hpp` 是 PTO 指令的"目录册"：149 条指令（u1-l1 已建立这个数字）每条在这里只有一个薄薄的模板函数包装（wrapper）。wrapper 不做任何计算，只做三件事：

1. 等待调用者传入的前置事件（见 4.2）；
2. 把操作数原样转发给后端实现 `*_IMPL`（见 4.3）；
3. 返回一个空的 `RecordEvent{}`，表示"本指令已发射，可以拿这个返回值去等别人"。

为什么要有 wrapper？因为**签名即契约**。只要 wrapper 的签名不变，后端实现怎么重写（比如 a5 的 int64 寄存器对仿真、a6 的专属 TMatmul）都不会波及任何调用方内核代码——这就是跨代际迁移的支点。

#### 4.1.2 核心流程

一条 PTO 指令从调用点到硬件的完整路径：

```text
用户内核代码
    │  TADD(dst, src0, src1);            ← 唯一需要写的调用
    ▼
pto_instr.hpp 公共 wrapper（本讲）
    │  ① PtoWaitEvents(events...)        ← 等前置事件
    │  ② MAP_INSTR_IMPL(TADD, ...)       ← 宏拼接转发
    ▼
TADD_IMPL（cpu / a2a3 / a5 各一份，由装配层决定用哪份）
    │  ③ TAddCheck：static_assert 类型/布局 + 运行期 valid 断言
    │  ④ 计算 elementsPerRepeat、RowStride 等派生参数
    ▼
TF 层结构体（如 TAdd<>）→ CCE 内建指令（如 vadd）     ← u4-l2 / u4-l3 精读
```

#### 4.1.3 源码精读

公共 API 的修饰宏定义在 type.hpp：

[include/pto/common/type.hpp:L21-L23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L21-L23) —— `PTO_INST` = `AICORE PTO_INLINE __attribute__((visibility("default")))`：公共指令带默认可见性，可被仓库外的算子工程链接；`PTO_INTERNAL` 少了 visibility 属性，仅供 PTO 内部实现层使用。**看修饰符就能判断一个函数是" ISA 面孔"还是"内部零件"**。

以 TADD 为代表的"标准三元指令"声明：

[include/pto/common/pto_instr.hpp:L174-L180](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L174-L180) —— TADD 的公共签名。四个要点：

- 模板参数 `TileDataDst / TileDataSrc0 / TileDataSrc1` 各自独立——允许 dst 与 src 是不同 Tile 特化（但 IMPL 内的 Check 会约束 dtype 一致）；
- 形参顺序固定为 `dst, src0, src1`，然后是 `WaitEvents&... events` 变参包**永远排在最后**（C++ 变参的硬性要求，也是 PTO 的统一约定）；
- 返回 `RecordEvent`（空标记类型，定义见 4.2.3）；
- 函数体只有两行：等待 + 转发。

TASSIGN 展示了"编译期重载"这一进阶模式：

[include/pto/common/pto_instr.hpp:L101-L105](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L101-L105) —— 运行时重载：`TASSIGN(obj, addr)`，把任意 Tile/GlobalTensor 绑到地址。

[include/pto/common/pto_instr.hpp:L107-L118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L107-L118) —— 编译期重载：`TASSIGN<Addr>(obj)` 模板整参形式，仅对 Tile/ConvTile 生效（`enable_if`），会在编译期触发 `tassign_static_check` 的地址越界/对齐检查（SA-0351~0354，u2-l4 已讲），最后仍委托给运行时路径。**同名多载、按需选择检查强度**，是公共 API 层的常见手法。

#### 4.1.4 代码实践

1. **实践目标**：验证"wrapper 只做等待+转发，不含计算"这一论断。
2. **操作步骤**：在 `pto_instr.hpp` 中任选 5 条指令的声明（建议：`TADD`、`TABS`、`TMUL`、`TTRANS`、`TMRGSORT`），逐个观察函数体，统计其中包含 `return {}` 以外逻辑的行。
3. **需要观察的现象**：绝大多数 wrapper 的函数体都只有 `detail::PtoWaitEvents(events...)` 和一行 `MAP_INSTR_IMPL(...)`；个别指令（如 `TPREFETCH_ASYNC`）直接返回 `*_IMPL` 的返回值。
4. **预期结果**：你能总结出"公共层无算法"的分层纪律；若发现某条指令在公共层写了逻辑（如 `SYNCALL` 的模式分派），思考它为什么例外（提示：分派本身与后端无关）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TADD` 的三个操作数模板参数要分开写，而不写成一个 `TileData`？

**答案**：因为 dst 与 src 允许是不同的 Tile 特化类型（形状、布局、Stride 都可能不同），实现层再用 `static_assert` 收紧 dtype 一致等约束。若合成一个模板参数，就强制三者完全同型，会把约束做死在签名层而失去灵活性。

**练习 2**：`TASSIGN` 两个重载的检查强度有何差别？

**答案**：运行时重载 `TASSIGN(obj, addr)` 只做绑定，不检查；编译期重载 `TASSIGN<Addr>(obj)` 借助模板整参在编译期触发 `tassign_static_check` 的越界/对齐静态断言（仅 Tile/ConvTile 可用），然后委托运行时路径。

### 4.2 WaitEvents 变参：把同步写进签名

#### 4.2.1 概念说明

u3-l1 教过 `set_flag`/`wait_flag` 原语；u3-l2 教过 `EventIdCounter` 自动发号。它们解决的是"事件怎么产生和编号"。本模块看另一半：**一条指令如何声明"我接受前置事件"**。

答案就是签名尾部那个变参包：

```cpp
template <..., typename... WaitEvents>
PTO_INST RecordEvent TXXX(...args..., WaitEvents&... events);
```

调用时把若干 `RecordEvent`（或其他事件对象）追加在操作数后面，wrapper 会先等它们全部完成再发射指令。这样"数据依赖"直接表达在调用点上，不必单独写一条 wait。

#### 4.2.2 核心流程

```text
调用：TADD(dst, src0, src1, evtA, evtB);
          │
          ▼
detail::PtoWaitEvents(evtA, evtB)     ← wrapper 内第一件事
          │  逐个展开：WaitAllEvents(evtA, evtB)
          ▼
MAP_INSTR_IMPL(TADD, dst, src0, src1) ← 注意：events 不转发给 IMPL
          │
          ▼
返回 RecordEvent{}                     ← 本指令的"完成凭据"
```

关键细节：**events 只在 wrapper 层被消费，不会传进 `*_IMPL`**。等待是发射前的排序动作，与实现无关，所以留在公共层。

#### 4.2.3 源码精读

变参的公共落点在 `pto_instr.hpp` 的 detail 命名空间：

[include/pto/common/pto_instr.hpp:L93-L97](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L93-L97) —— `PtoWaitEvents` 是内部桥接助手（注释明确说明：旧公共接口移除后供 PTO wrapper 与仓库内内核使用，**有意不作为公共 ISA**），它只是把变参包转交给 `WaitAllEvents`。

`RecordEvent` 与 `WaitAllEvents` 都定义在 event.hpp：

[include/pto/common/event.hpp:L297-L297](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/event.hpp#L297) —— `struct RecordEvent {};` 是空标记类型：它不携带数据，价值在于"出现在签名里"——让返回值可以被后续指令的 WaitEvents 变参接收，形成链式表达。

[include/pto/common/event.hpp:L341-L341](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/event.hpp#L341) —— `WaitAllEvents` 变参等待函数，是 `PtoWaitEvents` 的实现终点。

并非所有指令都接受 WaitEvents——对照两组签名：

[include/pto/common/pto_instr.hpp:L262-L268](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L262-L268) —— TLOAD 带 `WaitEvents&... events`：加载产生的数据会被后续计算消费，必须支持"先等别人、再被别人等"。

[include/pto/common/pto_instr.hpp:L270-L275](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L270-L275) —— **TPREFETCH 没有变参**：预取只是缓存预热提示，指令完成与否不影响正确性，自然不需要排序凭据（它仍返回 `RecordEvent`，但调用方通常不等它）。

有的指令还会用 `enable_if` 给变参加类型约束：

[include/pto/common/pto_instr.hpp:L296-L305](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L296-L305) —— TCMPS 用 `all_events_v<WaitEvents...>` 约束"尾部参数必须全是事件类型"，防止用户把标量误塞进变参被静默吞掉。

异步指令则返回另一种事件类型：

[include/pto/common/pto_instr.hpp:L287-L292](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L287-L292) —— `TPREFETCH_ASYNC` 返回 `comm::AsyncEvent` 而非 `RecordEvent`，且直接转发给 `TPREFETCH_ASYNC_IMPL`（异步完成需要上下文 `ctx.session` 才能等待，语义不同，u6 系列详述）。

#### 4.2.4 代码实践

1. **实践目标**：用签名本身判断指令的同步语义。
2. **操作步骤**：在 `pto_instr.hpp` 中用检索找出所有**不带** `WaitEvents` 变参的指令声明（如 `TASSIGN`、`TPREFETCH`、各类 `SET_*` 配置指令）。
3. **需要观察的现象**：这些指令要么不产生数据（地址绑定、参数配置），要么是性能提示（预取）。
4. **预期结果**：形成一条判断规则——**"产出会被别人消费的数据 → 带 WaitEvents；只改配置/给提示 → 不带"**。把检索结果按此规则分类填入表格。
5. 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`TADD(a, b, c, evt)` 中 `evt` 传给 `TADD_IMPL` 了吗？

**答案**：没有。`MAP_INSTR_IMPL(TADD, dst, src0, src1)` 只转发三个操作数；`evt` 在 wrapper 里被 `PtoWaitEvents` 消费完毕。等待是发射前的排序，属于公共层职责。

**练习 2**：为什么 `TPREFETCH` 不接受 WaitEvents 却仍返回 `RecordEvent`？

**答案**：预取的正确性不依赖它完成（未命中只是性能损失），所以无需前置排序；保留 `RecordEvent` 返回值使签名统一、并允许调用方在需要时（如统计）仍然持有它。

**练习 3**：`TPREFETCH_ASYNC` 为什么不返回 `RecordEvent`？

**答案**：异步预取的完成语义与同步指令不同——需要 `evt.Wait(ctx.session)` 携带会话上下文才能等待，所以返回 `comm::AsyncEvent`，并直接转发给 `TPREFETCH_ASYNC_IMPL` 而不走 `MAP_INSTR_IMPL` 拼接。

### 4.3 实现分发：MAP_INSTR_IMPL 宏与 _IMPL 拼接

#### 4.3.1 概念说明

公共 wrapper 如何找到正确的后端实现？答案是**预处理期拼接**：`MAP_INSTR_IMPL(TADD, ...)` 展开为对 `TADD_IMPL(...)` 的调用。`TADD_IMPL` 这个符号在全仓库有三份定义（cpu、a2a3、a5），**哪份被编译取决于装配层 include 了哪个头**（见 4.5）。这不是运行时多态，而是"用一个翻译单元只含一套实现"的编译期单选。

#### 4.3.2 核心流程

宏族按"CPU sim 与否"分两套定义：

```text
CPU sim（__CPU_SIM 已定义）:
  MAP_INSTR_IMPL(API, ...) = { 追踪作用域(API,...); API##_IMPL(...); }
                              ↑ PtoInstrTraceScope 记录指令调用序列

其余后端（NPU / CostModel）:
  MAP_INSTR_IMPL(API, ...) = API##_IMPL(...)      ← 纯拼接，零开销
```

变体后缀的含义：`_T` 带显式模板实参（`PTO_TEMPLATE_ARGS` 展开 `<...>`）、`_OUTS` 声明输出操作数个数（供追踪器区分输入输出）、`_ROLES` 声明角色标签——这些只在 CPU sim 的追踪分支有意义，其余后端全部塌缩为同一条拼接。

#### 4.3.3 源码精读

宏族的 CPU sim 定义：

[include/pto/common/pto_instr.hpp:L36-L45](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L36-L45) —— CPU sim 下 `MAP_INSTR_IMPL` 先构造 `PtoInstrTraceScope`（记录指令名与操作数，供 CPU 模拟器指令级追踪），再调用 `API##_IMPL`；`MAP_INSTR_IMPL_OUTS` 额外带输出计数。

[include/pto/common/pto_instr.hpp:L66-L76](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L66-L76) —— 非 CPU sim 下所有变体全部退化为一次拼接调用，无任何额外指令——**公共层在真实后端上是零成本的**。

"三份 TADD_IMPL 并存"的物证（u1-l5 结论的行号落实）：

- [include/pto/cpu/TAdd.hpp:L64-L64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L64-L64) —— CPU 模拟器版 `TADD_IMPL`。
- [include/pto/npu/a2a3/TAdd.hpp:L81-L81](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L81-L81) —— A2/A3 版。
- [include/pto/npu/a5/TAdd.hpp:L82-L82](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L82-L82) —— A5 版。

再看 a2a3 版 IMPL 的内部结构，理解"IMPL → TF → CCE"的下半程：

[include/pto/npu/a2a3/TAdd.hpp:L81-L96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L81-L96) —— `TADD_IMPL` 先调 `TAddCheck`（dtype 白名单、行主序约束的 `static_assert`，u2-l1 讲过 Check 层），再按 `BLOCK_BYTE_SIZE`/`REPEAT_BYTE` 推导 `elementsPerRepeat` 等派生参数，最后把 `dst.data()` 原始指针连同参数转发给 TF 层的 `TAdd<>` 结构体——后者最终封装 CCE 内建 `vadd` 指令（u4-l3 精读）。

至此四层抽象完整立起来：**User API（pto_instr.hpp wrapper）→ IMPL（各后端 `*_IMPL` + Check）→ TF（结构体层，CCE 友好形态）→ CCE 内建（vadd 等）**。本讲负责上半程，u4-l2 / u4-l3 负责下半程。

#### 4.3.4 代码实践

1. **实践目标**：亲手确认"拼接单选"机制。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "void TADD_IMPL" include/pto/`，应得到三处命中（cpu / a2a3 / a5）；
   - 再执行 `grep -n "TADD_IMPL" include/pto/npu/a6/header.hpp`，观察 A6 汇总头**没有** TAdd 专属文件——A6 直接复用 A5 的 TAdd 头（见 4.5.3）。
3. **需要观察的现象**：同名 `TADD_IMPL` 的多个定义从不出现在同一个翻译单元——装配层每个架构块互斥（`#ifdef PTO_NPU_ARCH_*`），CPU sim 块与 NPU 块也互斥。
4. **预期结果**：理解"多份定义、编译期单选"与 C++ 虚函数/函数指针的运行时分发截然不同：没有间接跳转开销，但也没有运行时切换能力。
5. grep 命令可在任意类 Unix 环境执行；若在 Windows 下可用任意支持正则的搜索工具替代。

#### 4.3.5 小练习与答案

**练习 1**：CPU sim 下 `MAP_INSTR_IMPL` 比其他后端多做了一件事，是什么？为什么只在那里做？

**答案**：构造 `PtoInstrTraceScope` 追踪作用域，记录指令调用序列。追踪需要真实的执行流与宿主 I/O，只适合 CPU 模拟器；NPU 上加追踪会污染性能，CostModel 关注的是指令计数而非逐步执行。

**练习 2**：如果有人把 a2a3 与 a5 的头同时 include 进一个翻译单元，会发生什么？

**答案**：两份 `TADD_IMPL` 定义冲突，重定义编译错误。这正是装配层用互斥的 `#ifdef PTO_NPU_ARCH_*` 块组织 include 的原因——保证任何宏组合下每个 `*_IMPL` 至多一份定义。

### 4.4 指令命名约定：从名字读出语义

#### 4.4.1 概念说明

PTO 的 149 条指令共用一套构词法。掌握它之后，看到陌生指令名就能猜出：操作什么（Tile/标量/GM）、算什么（算术/规约/搬运/矩阵）、什么变体（累加/异步/部分规约）。本模块把这些规律整理成速查表，并与 ISA 文档侧的约定（`docs/isa/conventions.md`）对齐。

#### 4.4.2 核心流程（构词法速查）

| 模式 | 含义 | 例子 |
| --- | --- | --- |
| `T` 前缀 | Tile 级指令（操作整个 tile） | `TADD`、`TLOAD` |
| `S` 后缀 | 标量变体：第二操作数是主机侧标量（`*S`） | `TEXPANDS`、`TORS` |
| `C` 变体 | 编码立即数变体（`*C`，文档约定） | 见各指令 ISA 页 |
| `_ACC` 后缀 | 累加：在输入累加器基础上累乘累加 | `TMATMUL_ACC` |
| `_BIAS` 后缀 | 带偏置项 | `TMATMUL_BIAS` |
| `_MX` 后缀 | MX 微缩放格式（数据 + scale tile） | `TMATMUL_MX` |
| `_ASYNC` 后缀 | 异步变体，返回 `AsyncEvent` | `TPUT_ASYNC`、`TPREFETCH_ASYNC` |
| `ROW/COL + SUM/MAX/MIN/PROD` | 按行/列规约 | `TROWSUM`、`TCOLMAX` |
| `ROW/COL + EXPAND` | 规约值按行/列广播展开 | `TROWEXPAND` |
| `PART + Arg` | 部分规约 / 带索引的 argmax/argmin | `TPARTMAX`、`TPARTARGMAX` |
| `SET/GET + 名词` | 配置类：写/读硬件状态，非数据计算 | `SET_QUANT_SCALAR` |
| `M` 前缀（MGather/MScatter） | 多 tile 聚散 | `MGATHER` |

#### 4.4.3 源码精读

文档侧约定（`*S` / `*C` 的权威定义）：

[docs/isa/conventions.md:L9-L9](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/conventions.md#L9) —— Scalar / immediate：主机侧标量或编码立即数，供 `*S` / `*C` 变体使用。这是命名约定在 ISA 文档层的落点。

S 后缀的签名实例——标量以 `typename TileData::DType` 出现：

[include/pto/common/pto_instr.hpp:L254-L260](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L254-L260) —— `TEXPANDS(dst, scalar, events...)`：把一个标量广播展开到 tile，第二操作数类型是 `typename TileData::DType`（从 tile 推导 dtype 的标量），这就是"S 后缀 = 标量在操作数里"的签名证据。

[include/pto/common/pto_instr.hpp:L2028-L2029](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L2028-L2029) —— `TORS(dst, src, scalar, events...)`：按位或的标量变体，同样模式。

`_ACC` 后缀的重载家族——TMATMUL_ACC 在同一个名字下有三个重载：

[include/pto/common/pto_instr.hpp:L701-L708](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L701-L708) —— 基础形态：`TMATMUL_ACC(cOut, cIn, a, b)`，显式区分输出累加器与输入累加器——`dst = cIn + a×b`，这就是"_ACC = 带初值的矩阵乘"的语义来源。

[include/pto/common/pto_instr.hpp:L711-L718](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L711-L718) —— UF-aware 形态：额外带 `AccPhase` 模板参（unit-flag 选择），转发到 `TMATMUL_ACC_IMPL<Phase>` 而非 `MAP_INSTR_IMPL` 拼接（模板参需显式传递）。

[include/pto/common/pto_instr.hpp:L720-L728](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L720-L728) —— 共享累加器形态：`TMATMUL_ACC(c, a, b)`，输入输出是同一个 tile，GEMM K 维累加循环最常用的写法（u5-l3 将实战）。

对照基础版 `TMATMUL`（覆盖语义，不带初值）：

[include/pto/common/pto_instr.hpp:L684-L690](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L684-L690) —— `TMATMUL(c, a, b)`：`c = a×b`。有无 `_ACC` 的差别就是"覆盖还是累加"。

conventions.md 还约定了每条指令文档页必须写清的三件事（操作数约束的文档基准）：

[docs/isa/conventions.md:L22-L30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/conventions.md#L22-L30) —— 有效区域语义：除非指令另行声明，迭代域取 `dst` 的 `(GetValidRow(), GetValidCol())`，区域外元素**未指定**（不许假设清零或保持）。

[docs/isa/conventions.md:L32-L34](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/conventions.md#L32-L34) —— 类型：指令页列出支持 dtype，CPU 模拟器可能是子集，以 `include/README.md` 状态表为准（u2-l1 的"CPU 跑通≠全后端合法"结论的文档依据）。

[docs/isa/conventions.md:L36-L41](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/conventions.md#L36-L41) —— 事件：示例中出现 `set_flag`/`wait_flag` 即代表目标后端要求的顺序约束。

#### 4.4.4 代码实践

1. **实践目标**：用构词法解码陌生指令。
2. **操作步骤**：从 `docs/PTOISA.md` 指令清单中挑 6 条你没读过的指令（建议含 `TSELS`、`TMATMUL_MX`、`TPARTARGMIN`、`TCOLEXPANDADD`、`SET_IMG2COL_RPT`、`TDEINTERLEAVE`），先只看名字写下猜测的语义，再打开对应 `docs/isa/*.md` 核对。
3. **需要观察的现象**：构词法的命中率（我们预计 5/6 以上）；猜错的多半是复合词（如 `EXPDIF`）。
4. **预期结果**：形成你自己的"名字 → 语义"反射，之后读内核源码不再需要逐条查文档。
5. 本实践为文档阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`TADDS` 与 `TADD`、`TMATMUL_ACC` 与 `TMATMUL` 各差在哪？

**答案**：`TADDS` 的第二操作数是标量（tile-scalar），`TADD` 是两个 tile；`TMATMUL_ACC` 在累加器初值上累加（`c = cIn + a×b` 或共享 c），`TMATMUL` 直接覆盖（`c = a×b`）。

**练习 2**：从签名上如何一眼区分 `TMATMUL_MX` 与 `TMATMUL`？

**答案**：`TMATMUL_MX` 多出 `aScaleMatrix` / `bScaleMatrix` 两个 scale tile 操作数（MX 格式 = 低精度数据 + 缩放因子），且各重载带 `Phase` 模板参——见 `pto_instr.hpp` L605 起的六个重载（u5-l6 详述）。

### 4.5 架构头文件组织：pto_instr_impl.hpp 装配枢纽

#### 4.5.1 概念说明

`pto_instr_impl.hpp` 自己不定义任何指令，它是**装配枢纽**：按架构宏把选中后端的全部 `*_IMPL` 头一次性拉进翻译单元。它解决的问题是——公共 wrapper 调用 `TADD_IMPL`，但 `TADD_IMPL` 的定义分散在几十个头文件里，必须有个地方"按需整批引入"。

本版本（`0dbecbe` → `be5ccb7`）该文件有两处关键变化，恰好都是"条件编译配对"主题的活教材：

1. **`__DAV_VEC__` 保护 TCvt 补上 `#endif`**：修复条件块吞头的装配事故；
2. **新增 `PTO_NPU_ARCH_A6` 块**：恢复 A6 架构指令头接入。

#### 4.5.2 核心流程

装配的完整决策树（与 u1-l5 的入口三段式衔接）：

```text
pto-inst.hpp 选定后端宏
    │  arch_macro.hpp: __NPU_ARCH__ → PTO_NPU_ARCH_A2A3/A5/A6/KIRIN*
    ▼
pto_instr_impl.hpp 依次检查（互斥块，每翻译单元至多命中一个 NPU 架构）：
    ├─ PTO_NPU_ARCH_A2A3 ──┬─ __COSTMODEL     → a2a3 精简头集（供 CostModel mock）
    │                      └─ 其余（真机）    → a2a3 全量头集
    ├─ PTO_NPU_ARCH_A5  ──┬─ __COSTMODEL     → a5 精简头集
    │                      └─ 其余（真机）    → a5 全量头集
    │                          └─ 其内：__DAV_VEC__ 才包含 TCvt（立即闭合）
    ├─ PTO_NPU_ARCH_A6      → a6/header.hpp（汇总头：专属 + 复用 a5）   ← 本版本新增
    ├─ PTO_NPU_ARCH_KIRIN9030 / KIRINX90 / KIRINDEV0000 → 各自汇总头
    ├─ __CCE_AICORE__ 且非 CPU sim / CostModel → 各架构 TPrefetchAsync 包装
    └─ __CPU_SIM            → pto/cpu/ 全量头集
```

`__DAV_VEC__` / `__DAV_CUBE__` 是编译器按编译目标核型预定义的宏：A5 之后的芯片区分向量核（DAV-VEC，AIV）与 Cube 核（DAV-CUBE，AIC），两类核的指令集不同。CPU 模拟器若无预设则两者都定义（模拟"全能核"）：

[include/pto/common/arch_macro.hpp:L14-L17](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_macro.hpp#L14-L17) —— CPU sim 下补定义 `__DAV_CUBE__` 与 `__DAV_VEC__`，所以模拟器上 TCvt 恒可用。

架构号到宏的翻译表（A6 的接入点）：

[include/pto/common/arch_macro.hpp:L19-L38](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_macro.hpp#L19-L38) —— `__NPU_ARCH__ == 9201` 时定义 `PTO_NPU_ARCH_A6`（同时 `PTO_COMM_NOT_SUPPORTED`，A6 暂不含通信扩展）；A2A3 对应 2201，A5 对应 3101/3510（3510 额外支持 URMA）。

#### 4.5.3 源码精读

**（1）`__DAV_VEC__` 保护 TCvt：为何必须立即闭合**

[include/pto/common/pto_instr_impl.hpp:L226-L228](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L226-L228) —— 当前版本：`#ifdef __DAV_VEC__` 包含 a5 的 `TCvt.hpp` 后**立即 `#endif`**。

为什么 TCvt 需要这层保护？看它的实现依赖：

[include/pto/npu/a5/TCvt.hpp:L22-L30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TCvt.hpp#L22-L30) —— A5 的 TCvt 使用 `__cce_simd` 命名空间的 `RoundAType` 等内建类型，这些 SIMD 内建只在向量核（DAV-VEC）编译目标下存在；在 Cube 核编译单元包含它会直接编译失败，所以装配层按核型裁剪。注意 a2a3 的 TCvt 无此保护（L39/L110/L132 直接包含）——A2/A3 的编译模型不区分两类 DAV 宏。

**旧版事故复盘**（`git show 0dbecbe:include/pto/common/pto_instr_impl.hpp` 可复核）：旧代码在 `#ifdef __DAV_VEC__` + `TCvt.hpp` 之后**没有写 `#endif`**，这个未闭合的 `#ifdef` 会一路"吞"到原本属于 `#ifdef __COSTMODEL` 的那个收尾 `#endif`（旧文件约 L311）。预处理器按文本配对不管注释语义，后果是：凡在 `__DAV_VEC__` 未定义的编译单元（如 Cube 核目标）里，从 `TStore.hpp` 到 `TDeInterleave.hpp` 的约 80 个 A5 指令头**全部被排除**——TSTORE、TMATMUL 等指令的 `*_IMPL` 集体缺失，整个 A5 后端在该单元失效。修复就是现在这三行：打开、包含、立即闭合。这正应了 u1-l5 的警示：**改装配头必须逐个核对条件配对**。

**（2）A5 全量头集的组织**

[include/pto/common/pto_instr_impl.hpp:L191-L197](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L191-L197) —— A5 块开头：同样按 `__COSTMODEL` 与否分精简/全量两支（CostModel 只需能 mock 数值的少量指令）。

[include/pto/common/pto_instr_impl.hpp:L314-L315](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L314-L315) —— A5 全量集的收尾：`#endif // __COSTMODEL` 与 `#endif` 成对——修复后这对配对不再被 `__DAV_VEC__` 的 `#ifdef` 抢占。

**（3）`PTO_NPU_ARCH_A6` 条件块：本版本新增**

[include/pto/common/pto_instr_impl.hpp:L317-L319](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L317-L319) —— A6 块只有一行 include：`pto/npu/a6/header.hpp`。与 a2a3/a5 的"逐头平铺"不同，A6 采用**汇总头**模式。

[include/pto/npu/a6/header.hpp:L20-L32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/header.hpp#L20-L32) —— 汇总头的内容与注释：A6 为 `TLoad`/`TExtract`/`TMatmul`/`TReshape`/`TQuant`/`TSync`/`SyncAll` 提供专属实现，其余指令（`TAssign`/`TAdd`/`TStore`）**头文件级复用 A5**——直接 include `pto/npu/a5/TAdd.hpp`。这就是 4.3.4 实践里"A6 搜不到 TADD_IMPL 专属文件"的原因：它编译进的就是 a5 那份。这种"新指令自研、成熟指令继承"的组织让新架构接入成本最低。Kirin 系列三个块（L321-329）同样走各自汇总头。

**（4）平台守卫的又一形态：TPrefetchAsync**

[include/pto/common/pto_instr_impl.hpp:L331-L343](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L331-L343) —— 异步 L2 预取头按架构分发（a2a3/a5 各一个包装头，共享同一 SDMA 实现，SQE 字段差异在实现内部用 `#ifdef PTO_NPU_ARCH_A5` 处理）；外层守卫 `__CCE_AICORE__ && !__CPU_SIM && !__COSTMODEL` 保证 CPU sim 与 CostModel 从下方自己的块拿到变体（CPU sim 下是 API 兼容的 no-op，见 L433）。这是注释里明确写清"为什么这样守卫"的范例——**平台守卫的意图要写进注释**。

[include/pto/common/pto_instr_impl.hpp:L345-L346](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L345-L346) —— `__CPU_SIM` 块开头：CPU 模拟器的全量头集，与 NPU 块互斥。

#### 4.5.4 代码实践

1. **实践目标**：亲手复核本版本的两处装配变化。
2. **操作步骤**：
   - 执行 `git diff 0dbecbe..be5ccb7 -- include/pto/common/pto_instr_impl.hpp`，在输出中找到（a）`TCvt.hpp` 下新增的 `#endif`；（b）新增的 `PTO_NPU_ARCH_A6` 三行；
   - 执行 `git show 0dbecbe:include/pto/common/pto_instr_impl.hpp | grep -n "DAV_VEC"`，确认旧版 `#ifdef __DAV_VEC__` 之后紧跟的是 `TStore.hpp` 而非 `#endif`；
   - 用 `awk '/#if(def|ndef)/{d++} /#endif/{d--} END{print d}' include/pto/common/pto_instr_impl.hpp` 检查当前文件条件编译深度是否归零（配对完整）。
3. **需要观察的现象**：diff 中 +12/-4 行的改动清单与上述两处变化对应；awk 输出 0。
4. **预期结果**：你能独立验证"修复了什么、新增了什么"，而不是仅凭本讲转述。git 只读命令在仓库内可直接执行；awk 配对检查是文本级检查，任何环境可用。
5. 若你的 git 环境缺少旧对象（浅克隆），`git show` 可能失败——此时以 GitHub 上旧 commit 的文件视图替代，或标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：旧版 `__DAV_VEC__` 未闭合时，为什么"向量核编译单元一切正常，Cube 核编译单元整个后端失效"？

**答案**：向量核单元里 `__DAV_VEC__` 已定义，`#ifdef` 分支内的头全部被包含，只是 `#endif` 的语义配对错位（配到了 COSTMODEL 的收尾）；Cube 核单元里 `__DAV_VEC__` 未定义，`#ifdef` 到下一个 `#endif` 之间的**全部内容被跳过**——而"下一个"恰好是约 80 个头之后的 COSTMODEL 收尾，于是 TStore 起全部缺失。

**练习 2**：A6 接入为什么用汇总头而不是像 a5 那样在装配层平铺几十个 include？

**答案**：A6 目前指令集尚小（专属 TSync/TLoad/TExtract/TMatmul/TReshape/TQuant 等，其余复用 A5）。汇总头把"哪些专属、哪些继承"的决策集中在一个 34 行文件里，装配层只写一行；后续 A6 指令扩充也只改汇总头，不动装配枢纽。

**练习 3**：`arch_macro.hpp` 中 A6 为什么同时定义 `PTO_COMM_NOT_SUPPORTED`？

**答案**：声明该架构暂不接入 PTO 通信扩展。装配层据此跳过通信头的包含（`pto_instr.hpp` L78-80 对通信头的包含即受此宏控制），让 A6 在指令集未齐的阶段也能编译通过。

## 5. 综合实践

把本讲五个模块串成一个任务——**给三条指令写"签名档案"，再当一次装配审计员**：

1. **签名档案**：在 `pto_instr.hpp` 中找到 `TADD`、`TMATMUL_ACC`、`TPREFETCH`，为每条整理一张卡片：
   - 公共签名（模板参数 + 形参列表，抄原文件并标注行号）；
   - 是否带 `WaitEvents` 变参、返回类型（`RecordEvent` 还是别的）；
   - 对照 `docs/isa/conventions.md` 与该指令的 `docs/isa/*.md` 页面，写出操作数约束（迭代域、dtype 支持、布局要求）与事件规则（示例中 `set_flag`/`wait_flag` 要求的顺序）；
   - 按 4.4 构词法给指令名"解码"，验证解码与文档描述一致。
2. **分发档案**：对这三条指令各回答：`*_IMPL` 有哪几份定义（grep 验证）？分别被装配层的哪个块包含？A6 上用的是哪份？
3. **装配审计**：按 4.5.4 的步骤复核 `__DAV_VEC__` 修复与 `PTO_NPU_ARCH_A6` 块，然后回答两个问题：
   - `__DAV_VEC__` 保护的 TCvt 头为何必须立即 `#endif` 闭合？（用旧版被吞头的范围量化说明）
   - `PTO_NPU_ARCH_A6` 条件块在装配流中处于什么位置、为什么只需一行 include？（对照 `a6/header.hpp` 的"专属 + 复用 A5"结构）
4. **预期产出**：三张指令卡片 + 一份分发清单 + 一段装配审计记录。全部材料基于当前 HEAD 的真实源码，每个结论都带 `文件:行号` 引用。

本实践为源码阅读 + git 只读操作型，不需要 NPU 或模拟器环境；其中 grep/git 命令在仓库内即可执行。

## 6. 本讲小结

- **签名即契约**：`pto_instr.hpp` 中每条指令的 wrapper 只做"等待前置事件 + 转发操作数 + 返回 `RecordEvent`"三件事，函数体无算法；`PTO_INST`（默认可见性）与 `PTO_INTERNAL` 的差别标记了公共 ISA 面孔与内部零件的边界。
- **WaitEvents 变参永远在签名尾部**，只在 wrapper 层被 `PtoWaitEvents`→`WaitAllEvents` 消费、不进 `*_IMPL`；产出会被消费的数据的指令才带变参（对照 TLOAD 有、TPREFETCH 无），异步变体则返回 `comm::AsyncEvent`。
- **分发靠预处理拼接**：`MAP_INSTR_IMPL(TADD,...)` → `TADD_IMPL(...)`，同名 `*_IMPL` 在 cpu/a2a3/a5 各一份，装配层互斥块保证编译期单选；完整层次为 User API → IMPL（含 Check）→ TF 结构体 → CCE 内建。
- **构词法可解码指令**：`S` 后缀 = 标量变体、`_ACC` = 累加、`_MX` = 微缩放、`_ASYNC` = 异步、`ROW/COL+规约名` = 轴规约、`SET/GET` = 配置类；`docs/isa/conventions.md` 是操作数约束与事件规则的文档基准。
- **装配枢纽的两个教训**：`__DAV_VEC__` 保护的条件块必须立即闭合（旧版缺 `#endif` 导致非向量核单元丢失约 80 个 A5 头）；新架构接入优先走汇总头模式（A6 = 专属六七件 + 头文件级复用 A5，装配层一行 include）。
- 本版本增量落点：`pto_instr_impl.hpp` 的 `#endif` 修复与 `PTO_NPU_ARCH_A6` 块（L317-319）是本讲相对旧版讲义的全部实质变化，其余签名约定与分发机制自上一版以来稳定。

## 7. 下一步学习建议

- **u4-l2（CPU 模拟器后端实现剖析）**：向下钻一层，看 `include/pto/cpu/TAdd.hpp` 的 `TADD_IMPL` 如何用并行 for 循环、布局偏移与 `NPUMemoryModel` 模拟出指令语义——本讲 4.3 的下半程。
- **u4-l3（NPU a2a3 后端实现剖析）**：对照 CPU 版走读 `npu/a2a3/TAdd.hpp` 的 `AddOp`→`vadd` 链路与 `TAddCheck` 的三条 `static_assert`，并理解 `TRem` 的掩码模式。
- **u5-l1（ST 测试体系）**：学如何为一条指令建完整的 ST 用例四件套——那是验证你对签名与约束理解的最好工具。
- 延伸阅读：`include/pto/README.md`（模块职责）、`docs/isa/README.md`（ISA 文档索引）；想看 A6 专属指令的实现可浏览 `include/pto/npu/a6/` 下 `TMatmul.hpp` 等少数几个文件。
