# 二次开发：为 PTO 新增一条指令

## 1. 本讲目标

学完本讲，你应当能够：

1. 背出（或推导出）新增一条 PTO 指令需要触达的**全部文件清单**——8 处必改、2 处可选。
2. 理解「公共 API 声明 → Op 登记 → 后端实现 → 汇总头挂载 → ISA 文档 → ST 用例 → 状态表登记」这条**四位一体开发闭环**，以及每一步漏掉会发生什么。
3. 以 TADD 为模板，独立完成一条新指令（本讲以 THYPOT 勾股指令为例）的**CPU 仿真实现 + ST 用例验证**。
4. 按 `CONTRIBUTING.md` 的规范走完贡献流程：Issue 设计讨论 → 本地开发与格式检查 → PR 提交与评审。

本讲是整个学习手册的收官单元之一：它不再引入新的编程模型概念，而是把前面 10 个单元学到的所有积木（指令分层、事件、有效区、ST 用例）组装成一次真实的二次开发。

## 2. 前置知识

本讲默认你已完成 u3-l4（CPU 仿真实现剖析）和 u10-l1（测试体系）。核心前置概念快速回顾：

- **三段式指令结构**：每条指令由「公共 API 薄壳（common 层）+ 各后端 `*_IMPL` 实现（cpu / npu/<arch> 目录）+ 汇总头（pto_instr_impl.hpp）」组成。公共 API 里 `TSYNC(events...)` 折叠等待事件，`MAP_INSTR_IMPL` 宏把 `TADD(...)` 拼接转发成 `TADD_IMPL(...)`，返回空的 `RecordEvent`。
- **有效区（valid region）**：指令只在 dst 的 `GetValidRow()/GetValidCol()` 矩形内定义语义，CPU 实现的循环边界必须取自有效区。
- **ST 用例四件套**：`<op>_kernel.cpp`（被测 kernel + 显式模板实例化）、`main.cpp`（gtest 入口 + golden 比对）、`gen_data.py`（numpy 独立造数算 golden）、`CMakeLists.txt`（一行注册）。
- **策略模式模板（NPU 侧）**：指令文件只需提供约 15 行的策略类（如 `AddOp`，映射到 `vadd` intrinsic），遍历编排放公共模板 `TBinOp.hpp`。

本讲的贯穿示例是**勾股指令 THYPOT**，数学定义为：对有效区内每个元素，

\[ \mathrm{dst}_{i,j} = \sqrt{\mathrm{src0}_{i,j}^{2} + \mathrm{src1}_{i,j}^{2}} \]

即 NumPy 的 `np.hypot`。选它是因为：语义简单（逐元素二元）、CPU 侧可一行实现（`std::hypot`）、而 NPU 侧大概率没有 1:1 intrinsic——恰好能同时演示「直接映射」和「组合实现」两种设计决策。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|---|---|---|
| `include/pto/common/pto_instr.hpp` | 全部指令的公共 API 薄壳 | 新指令 API 的声明模板 |
| `include/pto/common/event.hpp` | `Op` 枚举与指令→流水线登记 | 新指令的事件系统登记点 |
| `include/pto/common/pto_instr_impl.hpp` | 按「架构 × 后端」汇总 include 实现头 | 新实现头的挂载点 |
| `include/pto/cpu/TAdd.hpp` | TADD 的 CPU 仿真实现 | CPU 实现的最佳模板 |
| `include/pto/cpu/ElementOp.h` / `ElementTileOp.h` | Element 仿真骨架 | 新指令的低成本替代实现路径 |
| `include/pto/npu/a2a3/TAdd.hpp` | TADD 的 NPU 真机实现 | NPU 实现的四层结构模板 |
| `docs/isa/TADD.md`、`docs/isa/README.md` | ISA 文档与索引 | 文档规范模板 |
| `include/README.md` | 逐指令后端支持状态表 | 清单登记点 |
| `tests/cpu/st/testcase/tadd/` | TADD 的 ST 用例四件套 | 测试闭环模板 |
| `tests/cpu/st/testcase/CMakeLists.txt` | 用例集合与 `ALL_TESTCASES` 注册 | 新用例的构建接入 |
| `tests/run_cpu.py`、`tests/validate_op_coverage.py` | CPU 运行入口与覆盖守护脚本 | 验证与防漏检 |
| `CONTRIBUTING.md` | 贡献流程与交付物清单规范 | 流程依据 |

## 4. 核心概念与源码讲解

### 4.1 实现清单：一条新指令要触碰的全部文件

#### 4.1.1 概念说明

在 PTO 里，「一条指令」不是一个小时能写完的单点函数，而是一个**横切仓库 5 类文件的交付物**：

- **接口层**（common）：公共 API + 事件系统登记；
- **实现层**（cpu / npu/<arch>）：至少一个后端的 `*_IMPL`；
- **装配层**：汇总头把实现头挂进正确的「架构 × 后端」编译分支；
- **文档层**：ISA 文档 + 索引 + 状态表；
- **测试层**：ST 用例四件套 + 用例集合注册。

`CONTRIBUTING.md` 把这套交付物结构写成了正式约定——新算子评审通过后，SIG 会给你分配一个类别路径（如 `include/pto/npu/a5`），交付目录按最小必需结构组织。清单本身就是评审时对照的验收标准。

#### 4.1.2 核心流程

新增指令的完整流程（以 THYPOT 为例的伪代码）：

```text
第一步 语义定稿
    确定：操作数个数与类型、有效区语义、落在哪条流水线（THYPOT 是逐元素 → PIPE_V）
第二步 接口层登记（common，2 个文件）
    pto_instr.hpp:  THYPOT(dst, src0, src1, events...) { TSYNC; MAP_INSTR_IMPL(THYPOT, ...); return {}; }
    event.hpp:      Op 枚举加 THYPOT；PTO_DEFINE_OP_PIPE(Op::THYPOT, PIPE_V)
第三步 实现层（至少 CPU 一个文件）
    方案 A：新建 include/pto/cpu/THypot.hpp，仿照 cpu/TAdd.hpp 手写两层
    方案 B：ElementOp.h 加 OP_HYPOT 枚举 + 一行语义特化，ElementTileOp.h 一行 BINARY_OP_DEF
第四步 装配层（1 个文件，多处）
    pto_instr_impl.hpp：在 __CPU_SIM 块加 #include "pto/cpu/THypot.hpp"
                        （NPU 实现就绪后再挂 a2a3 / a5 对应分支）
第五步 文档层（3 个文件）
    docs/isa/THYPOT.md（正文）+ docs/isa/README.md（索引行）+ include/README.md（状态表行）
第六步 测试层（5 个文件）
    tests/cpu/st/testcase/thypot/ 四件套 + 用例集合 CMakeLists 的 ALL_TESTCASES 加一行
第七步（可选）CostModel 层
    lightweight_costmodel.hpp 登记算子与造价系数
```

调用链回顾（承接 u2-l4）：用户 kernel 调 `THYPOT(...)` → `MAP_INSTR_IMPL(THYPOT, dst, src0, src1)` 预处理期拼接成 `THYPOT_IMPL(dst, src0, src1)` → 该符号由 `pto_instr_impl.hpp` 按 `__CPU_SIM` / `PTO_NPU_ARCH_*` 宏选中的那个实现头提供。**新指令只要三层名字严格一致（THYPOT / THYPOT_IMPL / 文件名），装配就是自动的。**

#### 4.1.3 源码精读

**① 公共 API 声明——一切从这三行开始**

宏定义与 TADD 的 API 薄壳（[include/pto/common/pto_instr.hpp:L23](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L23) 把 `TADD` 拼接成 `TADD_IMPL`；[include/pto/common/pto_instr.hpp:L112-L118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L112-L118) 是 TADD 的完整公共 API：等待可变参数事件 → 转发 IMPL → 返回空 `RecordEvent`）。新指令在这里加一个同构模板即可：

```cpp
// 示例代码：THYPOT 的公共 API（仿照 TADD）
template <typename TileDataDst, typename TileDataSrc0, typename TileDataSrc1, typename... WaitEvents>
PTO_INST RecordEvent THYPOT(TileDataDst& dst, TileDataSrc0& src0, TileDataSrc1& src1, WaitEvents&... events)
{
    TSYNC(events...);
    MAP_INSTR_IMPL(THYPOT, dst, src0, src1);
    return {};
}
```

**② Op 枚举与流水线登记——最容易被漏掉的一步**

[include/pto/common/event.hpp:L21-L27](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L21-L27) 是 `Op` 枚举开头，`TADD` 在 L27；[include/pto/common/event.hpp:L160-L169](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L160-L169) 定义了默认 `OpPipeEntry`（未登记的 Op 一律落到 `PIPE_ALL`）与 `PTO_DEFINE_OP_PIPE` 特化宏；[include/pto/common/event.hpp:L176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L176) 把 `Op::TADD` 登记到 `PIPE_V`。

为什么要登记？消费方在 NPU 侧：[include/pto/npu/a2a3/TSync.hpp:L24-L28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L24-L28) 的 `GetPipeByOpForA3` 查 `OpPipeEntry<op>::pipe`，供 `TSYNC<OpCode>()` 单流水线屏障（L31-L42）和对象风格 `Event<SrcOp, DstOp>` 推导流水线。**漏登记的后果**：新指令拿到的默认值是 `PIPE_ALL`，一旦用户写 `Event<THYPOT, TSTORE>`，真机编译期就会命中 TSync.hpp L67-L68 的 `"DstOp are invalid."` 静态断言。CPU 仿真下事件是空桩，这个错误在 CPU 上根本暴露不出来——这正是「CPU 只验功能」纪律的又一例证。

**③ CPU 仿真实现——两种姿势任选**

姿势 A（独立文件，CONTRIBUTING 推荐的标准布局）：[include/pto/cpu/TAdd.hpp:L19-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L19-L61) 是循环体 `TAdd_Impl`：按「分形与否 × 行/列主序」四路 `if constexpr`，非分形走裸指针 + `PTO_CPU_VECTORIZE_LOOP` 向量化，分形走 `GetTileElementOffset` 坐标映射，行级并行交给 `cpu::parallel_for_rows`；[include/pto/cpu/TAdd.hpp:L63-L69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L63-L69) 是 `TADD_IMPL` 薄壳：从 dst 取有效区、剥出 `.data()` 指针转发。

姿势 B（骨架宏，一行语义）：[include/pto/cpu/ElementOp.h:L21-L86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L21-L86) 的 `ElementOp` 枚举列出全部逐元素语义；[L88-L91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L88-L91) 的默认特化 `assert(false)` 防止静默错算；[L93-L98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L93-L98) 是 `OP_ADD` 的语义特化（一行数学）。数学函数要走 double 中转的既定范式见 [include/pto/cpu/ElementOp.h:L163-L174](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L163-L174) 的 `OP_POW` 特化（`static_cast<double>` 进、算完再 `static_cast<DType>` 出，避免 half 下精度陷阱）。最后在 [include/pto/cpu/ElementTileOp.h:L97-L102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L97-L102) 的 `BINARY_OP_DEF` 宏一行实例化出 `THYPOT_IMPL`（现有用法见 [L111-L124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L111-L124)）。注意骨架的遍历层 [L18-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L18-L58) 支持异构 tile 类型但没有向量化提示——长尾指令用姿势 B，热路径指令用姿势 A。

**④ 装配层——把实现头挂进正确的编译分支**

[include/pto/common/pto_instr_impl.hpp:L359-L370](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L359-L370) 是 `#ifdef __CPU_SIM` 块（`pto/cpu/TAdd.hpp` 在 L370），CPU 实现头挂这里；NPU 侧有三个互斥分支要留意——a2a3 的 CostModel 分支（[L18-L23](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L18-L23)，`PTO_NPU_ARCH_A2A3 && __COSTMODEL`）、a2a3 真机分支（[L82-L85](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L82-L85)，`TAdd.hpp` 在 L85）、a5 分支（[L197-L207](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L197-L207)）。新指令只实现 CPU 时只挂第一处；将来补 NPU 实现时，**别忘了 CostModel 分支也要挂**（CostModel 复用 NPU 实现头），否则 `__COSTMODEL` 后端编译会缺符号。

**⑤ NPU 真机实现——四层结构模板（本讲只规划、不实现）**

[include/pto/npu/a2a3/TAdd.hpp:L20-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L20-L32) 第一层：`AddOp` 策略类，两个 `BinInstr` 重载把语义映射到 `vadd` intrinsic（两个重载只在 repeatStride 传法上不同）；[L34-L54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L34-L54) 第二层：`__tf__` 设备函数，把 tile 降级成 `__ubuf__` 指针后交给 `BinaryInstr` 公共编排；[L56-L78](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L56-L78) 第三层：`TAddCheck` 契约检查——dtype 白名单（L62-L66）、仅行主序（L67-L69）、src 与 dst 有效区一致（L70-L77）；[L80-L94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L80-L94) 第四层：`TADD_IMPL` 装配，从 `BLOCK_BYTE_SIZE`/`REPEAT_BYTE` 推出 `blockSizeElem`/`elementsPerRepeat`（L85-L86），从 tile 类型取 `RowStride`（L88-L90）。

THYPOT 在 NPU 侧的设计决策点：A2/A3 向量流水线是否存在 1:1 的 hypot intrinsic——**待确认**。若没有，就要按 u4-l1 中 TFMOD 的先例走组合实现（如 `vmul`→`vmul`→`vadd`→`vsqrt` 多步组合），此时接口必须增加 tmp 暂存操作数、逐行 `pipe_barrier(PIPE_V)`，且**公共 API 签名要同步加 tmp**（CPU 与 NPU 的 `*_IMPL` 签名必须逐字一致，tmp 只能两端都带上、CPU 端 `(void)tmp` 忽略，参考 [include/pto/cpu/ElementTileOp.h:L151-L158](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L151-L158) TREM 的处理方式）。这是「先定 NPU 通路、再冻结公共接口」的原因——接口一旦合入就很难改。

**⑥ 可选：CostModel 登记位**

若希望 `__COSTMODEL` 后端能估算新指令周期：[include/pto/costmodel/lightweight_costmodel.hpp:L27](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L27) 的算子枚举、[L190](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L190) 的名字表、[L256-L257](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L256-L257) 的 `TryEstimateFormulaCycles` 分发各加一项；API 薄壳在 [include/pto/costmodel/pto_instr.hpp:L309-L312](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/pto_instr.hpp#L309-L312) 有对应模板。系数缺失时回退兜底公式并打 WARN，不阻塞功能——所以这一步可以后补。

#### 4.1.4 代码实践

1. **实践目标**：用一条已有指令验证「实现清单」的完备性——清单上每一类文件都能被 grep 命中，就说明没有漏项。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "TGATHER" include/pto --include=*.hpp --include=*.h | grep -v Binary`，把命中文件按「API / Op 登记 / 实现 / 装配 / 文档」分类；
   - 再执行 `grep -rln "tgather" tests/cpu/st/testcase/CMakeLists.txt tests/cpu/st/testcase/tgather/ 2>/dev/null` 确认测试层命中。
3. **需要观察的现象**：`include/pto/common/pto_instr.hpp`、`include/pto/common/event.hpp`、`include/pto/common/pto_instr_impl.hpp`、`include/pto/cpu/TGather.hpp`、`include/pto/npu/a2a3/TGather.hpp`、`docs/isa/TGATHER.md`、`include/README.md`、`tests/cpu/st/testcase/tgather/` 应当全部出现在结果里。
4. **预期结果**：命中文件与 4.1.2 清单的 ①~⑧ 一一对应；若某类缺失（例如某指令没有 CPU 实现），对应状态表里该列应标 `TODO`——清单与状态表互为印证。
5. 本实践为纯只读 grep，结果可当场核验。

#### 4.1.5 小练习与答案

**练习 1**：如果只做了清单 ①③（API + CPU 实现），忘了 ④（挂载 pto_instr_impl.hpp），会在哪个阶段报什么错？

**答案**：编译期报错。`MAP_INSTR_IMPL(THYPOT, ...)` 在预处理期展开成 `THYPOT_IMPL(...)`，而该符号所在的实现头从未被 include，模板函数未定义——报「use of undeclared identifier 'THYPOT_IMPL'」或链接期 undefined reference（取决于调用点是否触发两阶段查找），总之 CPU 用例无法编译通过。

**练习 2**：为什么 `PTO_DEFINE_OP_PIPE(Op::THYPOT, PIPE_V)` 漏掉时，CPU 仿真测试照样全绿？

**答案**：CPU 仿真后端把事件全部做成空桩、单线程按序执行（u2-l3 结论），`OpPipeEntry` 的查询只发生在 NPU 侧 `GetPipeByOpForA3`（TSync.hpp L24-L28）和 `TSYNC<OpCode>()`、`Event<SrcOp,DstOp>` 的编译期推导里。默认特化返回 `PIPE_ALL` 不参与 CPU 执行路径，只有真机编译用对象风格事件时才命中 static_assert——所以这类登记错误必须靠清单纪律而非 CPU 测试兜底。

**练习 3**：新指令若 NPU 侧需要 tmp 暂存 tile（如组合实现的 TFMOD），公共 API 应该怎么设计？

**答案**：tmp 必须写进公共 API 签名（`THYPOT(dst, src0, src1, tmp, events...)`），因为 CPU 与 NPU 的 `*_IMPL` 签名要逐字一致才能共用同一个 `MAP_INSTR_IMPL` 转发；CPU 端不真正使用 tmp 时以 `(void)tmp` 显式忽略（仓库先例：ElementTileOp.h 的 TREM_IMPL，L151-L158）。

### 4.2 文档规范：写一份合格的 ISA 文档

#### 4.2.1 概念说明

ISA 文档是 PTO 指令的「法定说明书」：它同时服务三类读者——写 kernel 的开发者（查语义与约束）、上层框架的对接者（查汇编形态）、以及评审者（对照实现查一致性）。文档不是代码注释的复述，而是**契约**：文档承诺的每个约束都应当能在实现里找到对应的 `static_assert` / `PTO_ASSERT`，反之亦然。`CONTRIBUTING.md` 把 `docs/isa/${op_name}.md` 列为交付物第一项。

#### 4.2.2 核心流程

TADD.md 的八段式结构就是标准模板：

```text
1. # 指令名            —— 一级标题即指令名
2. Tile Operation Diagram —— ../figures/isa/<OP>.svg 示意图（惯例配图）
3. Introduction        —— 一句话语义
4. Math Interpretation —— 逐元素数学公式（$$ ... $$）
5. Assembly Syntax     —— 三级汇编形态：同步形式 / AS Level 1 (SSA) / AS Level 2 (DPS)
6. C++ Intrinsic       —— 公共 C++ API 签名，注明声明于 include/pto/common/pto_instr.hpp
7. Constraints         —— 分架构列实现检查（A2A3 / A5 各自的 dtype 白名单、布局、有效区约定）
8. Examples           —— Auto 与 Manual 两种模式的可编译示例 + ASM 形态示例
```

写作顺序建议：先写 Math 与 Constraints（这两段直接决定实现与测试），再补示例；Constraints 要**按架构分小节**，因为同一指令在 a2a3 与 a5 的 dtype 支持面不同。

#### 4.2.3 源码精读

以 TADD.md 全文为样板逐段看：

- **示意图与简介**：[docs/isa/TADD.md:L4-L10](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L4-L10) 引用 `../figures/isa/TADD.svg` 并用一句话说明「Elementwise add of two tiles」。配图放在 `docs/figures/isa/` 目录（该目录已存在，内含 TADD.svg 等大量指令图）。
- **数学解释**：[docs/isa/TADD.md:L12-L16](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L12-L16) 逐元素公式 + 明确「valid region 内」这个定义域限定——这句必须写，它对应实现里以 `dst.GetValidRow()/GetValidCol()` 为循环边界。
- **汇编语法**：[docs/isa/TADD.md:L18-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L18-L35) 给出同步形式与 SSA/DPS 两级中间表示——这是 PyPTO、PTOAS 等上层工具对接的接口，新指令若计划被编译器后端识别，这三段不可省。
- **C++ Intrinsic**：[docs/isa/TADD.md:L37-L44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L37-L44) 签名必须与 pto_instr.hpp 里的实际声明逐参数一致（含可变参数 `WaitEvents&... events`）。
- **约束**：[docs/isa/TADD.md:L46-L55](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L46-L55) 分「Implementation checks (A2A3)」「Implementation checks (A5)」「Valid region」三小节——前两小节正对应 npu/a2a3/TAdd.hpp L62-L69 的 static_assert 白名单与行主序检查，A5 小节对应 a5 实现里更宽的 dtype 集合。**这就是文档-代码一致性的样板：每条约束都能在某个实现头里指认一行断言。**
- **示例**：[docs/isa/TADD.md:L57-L88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L57-L88) Auto 模式直接声明 Tile 即用；Manual 模式先 `TASSIGN` 绑地址再调用——两种模式各一段，读者照抄即可编译。
- **ASM 形态示例**：[docs/isa/TADD.md:L90-L115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L90-L115) 展示 Auto/Manual 落到 PTO Assembly 的样子，说明 Manual 下资源绑定指令与计算指令的先后关系。

写完正文还要做两处**索引登记**：[docs/isa/README.md:L24-L25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/README.md#L24-L25) 的分类索引（THYPOT 应插入「Elementwise (Tile-Tile)」小节，按字母序放在 THISTOGRAM 附近）；以及 4.3 节要讲的 [include/README.md:L28-L39](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L28-L39) 后端状态表（表头在 L28-L37，TADD 行在 L39）。状态表是新指令的「户口」：`| [THYPOT](../docs/isa/THYPOT.md) | Yes | TODO | TODO | TODO | TODO | TODO |`——只做 CPU 时只把 CPU 列置 Yes，其余 TODO，诚实反映支持面。

#### 4.2.4 代码实践

1. **实践目标**：起草 `docs/isa/THYPOT.md`，做到 Constraints 段与规划的实现断言一一对应。
2. **操作步骤**：复制 TADD.md 为模板；改标题与公式为 \( \mathrm{dst}_{i,j} = \sqrt{\mathrm{src0}_{i,j}^{2} + \mathrm{src1}_{i,j}^{2}} \)；Introduction 写「Elementwise hypot（square root of sum of squares）of two tiles」；C++ Intrinsic 段抄 4.1.3 ① 中你设计的 API 签名；Constraints 段写「仅支持 float/half（建议，勾股是浮点语义）、行主序、src 有效区须与 dst 一致」；Examples 段把 TADD 示例中的 `TADD(dst, src0, src1)` 换成 `THYPOT(dst, src0, src1)`。
3. **需要观察的现象**：自查 Constraints 段的每一条，能否在（计划中的）CPU/NPU 实现里指认一行对应的 `static_assert` 或检查；Introduction/Math 是否让一个从未见过 hypot 的读者明白语义。
4. **预期结果**：八段结构齐全、公式含 valid region 限定、示例可编译（在你完成综合实践后回头验证）。
5. 示意图 svg 可暂缺（正式贡献前补齐到 `docs/figures/isa/THYPOT.svg`），正文图注可先保留。

#### 4.2.5 小练习与答案

**练习 1**：TADD.md 的 Constraints 把 A2A3 与 A5 的 dtype 白名单分成两小节，为什么不合成一条？

**答案**：因为「指令 × 后端 × 架构」是三维坐标（u1-l2 结论）：a2a3 实现的 static_assert 只放行 `int32_t/int16_t/half/float` 等，而 a5 实现支持更宽的集合（含 uint 系、bfloat16_t、int8 等）。文档分节陈述才能与各自实现头里的断言精确对应，避免读者拿着 A5 的合法 dtype 在 A2 上编译失败。

**练习 2**：Math Interpretation 里「For each element (i, j) in the valid region」这半句删掉会怎样？

**答案**：语义就错了——实现只保证有效区内正确（CPU 循环边界取 `GetValidRow()/GetValidCol()`，NPU 靠 repeat/mask 裁剪），有效区外的 dst 内容是未定义的。删掉这半句，读者会以为指令计算整个容量形状，尾块场景（全局形状不是 tile 形状整数倍）就会产生对垃圾数据的错误期望。

### 4.3 测试闭环：ST 用例与守护脚本

#### 4.3.1 概念说明

PTO 的测试闭环是「**golden 镜像比对**」：C++ 实现产出 `output.bin`，`gen_data.py` 用 numpy 独立算出 `golden.bin`，`main.cpp` 逐元素比对。实现与 golden 是互为镜像的两份独立代码——**改语义必须成对修改**，否则测试要么误报、要么假绿。一条新指令至少要有一个 CPU ST 用例；用例没接入集合清单会被守护脚本判为覆盖缺失。

#### 4.3.2 核心流程

```text
运行一条 CPU ST 用例的完整链路：
python3 tests/run_cpu.py -t thypot
    └─ cmake 配置期：testcase/CMakeLists 的 foreach 只 add_subdirectory(匹配 -t 的用例)
         └─ 用例目录内 CMakeLists 一行 pto_cpu_sim_st(thypot) 生成 gtest 可执行
    └─ 运行 gen_data.py：按 case 参数表生成 testcases/THYPOTTest.case_*/{input*.bin, golden.bin}
    └─ 运行 gtest：main.cpp 读 input → Launch → 写 output.bin → 与 golden 做 ResultCmp
守护脚本（提交前）：
    tests/validate_op_coverage.py    —— 对照 run_st.sh 检查 NPU 树用例是否漏登记
    tests/validate_testcase_names.py —— 检查 TEST_F 名与 gen_data 目录名是否逐字符一致
```

命名是**三方契约**：gtest 的 `Suite.Case` 名、gen_data.py 生成的数据目录名、main.cpp 里拼出的 `../Suite.Case` 寻址路径必须完全一致，任何一方改动都要同步另外两方。

#### 4.3.3 源码精读

**kernel 侧**：[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:L17-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L17-L43) 是被测 kernel：声明动态 shape/stride 的 GlobalTensor 与 Tile、`TASSIGN` 绑三块片上地址（L26-L28）、`TLOAD` 两路（L34-L35）、`set_flag/wait_flag` 事件对（L36-L40）、`TADD` 计算（L38）、`TSTORE` 写回（L41）。新指令用例只需把 L38 换成新指令调用。[L45-L52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L45-L52) 的 `LaunchTAdd` 是 host 侧入口（aclFloat16 在此转成 half 调用）；[L54-L62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L62) 是**显式模板实例化清单**——每个 dtype×shape 组合一行，漏一行对应 TEST_F 就链接失败；`CPU_SIM_BFLOAT_ENABLED`（L59-L62）示范了可选 dtype 用宏门控。

**main 侧**：[tests/cpu/st/testcase/tadd/main.cpp:L27-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L27-L34) 的 `GetGoldenDir` 从 gtest 运行时取 `Suite.Case` 名拼出 golden 目录（三方契约的消费端）；[L39-L91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L39-L91) 是测试主体：acl 初始化 → host/device 内存 → 读 input → `Launch` → 回拷写 `output.bin`（L70）→ 读 golden 比对 `ResultCmp<T>(golden, devFinal, 0.001f)`（L88）；[L93-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/main.cpp#L93-L96) 四个 `TEST_F` 用例名与 gen_data 的目录名一一对应。

**golden 侧**：[tests/cpu/st/testcase/tadd/gen_data.py:L21-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38) 生成输入（randint 1..10 转 dtype）并按有效区切片计算 `golden = (input1 + input2)[:row_valid,:col_valid]`；[L53-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L53-L64) 的 `generate_case_name` 拼出 `TADDTest.case_{dtype}_{g}x{g}_{t}x{t}_{v}x{v}` 目录名（三方契约的生产端）；[L76-L83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/gen_data.py#L76-L83) 是 case 参数表——**新 dtype/形状覆盖加在这里，同时要在 kernel 显式实例化和 main 的 TEST_F 各加一行**，三处联动。

**构建注册**：用例目录内 [tests/cpu/st/testcase/tadd/CMakeLists.txt:L10](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadd/CMakeLists.txt#L10) 只有一行 `pto_cpu_sim_st(tadd)`；该函数定义在集合清单 [tests/cpu/st/testcase/CMakeLists.txt:L13-L37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L13-L37)（自动把 `main.cpp` 与可选的 `<name>_kernel.cpp` 组成可执行并接 gtest）；`tadd` 登记在 `ALL_TESTCASES` 列表（[L39-L46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L39-L46)，tadd 在 L46），末尾的 foreach（[L172-L176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L172-L176)）按 `TEST_CASE` 变量过滤后 `add_subdirectory`——这正是 `run_cpu.py -t` 定向构建的实现机制。

**运行与守护**：[tests/run_cpu.py:L455-L456](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L455-L456) 定义 `-t`（构建期定向单个用例，如 `tadd`）与 `-g`（gtest 运行期过滤，如 `'TADDTest.case_float_64x64_64x64_64x64'`）。[tests/validate_op_coverage.py:L11-L23](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L11-L23) 的职责是对照 `run_st.sh` 找出「有 main.cpp 但没接入执行脚本」的用例（覆盖 NPU 树三套目录，见 [L32-L36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/validate_op_coverage.py#L32-L36)）；命名契约由 `validate_testcase_names.py` 守护（u10-l1 已精读）。

#### 4.3.4 代码实践

1. **实践目标**：跑通 TADD 基线，确认你的环境能完整走「构建 → 造数 → 比对」链路，再做改造。
2. **操作步骤**：在仓库根目录执行 `python3 tests/run_cpu.py -t tadd --verbose`；成功后复制 `tests/cpu/st/testcase/tadd/` 四个文件到 `thypot/`（仅作草稿，不注册），列出所有需要替换命名的位置。
3. **需要观察的现象**：verbose 输出里应能看到 cmake 增量配置、`gen_data.py` 生成的 `TADDTest.case_*` 目录、gtest 的 4 个（bf16 开启则 5 个）case 通过、`ResultCmp` 无失败。
4. **预期结果**：`[ PASSED ] 4 tests`（float32/int32/int16/half 各一；bf16 case 取决于 `PTO_CPU_SIM_ENABLE_BF16` 环境变量）。改造草稿需替换的命名点：`tadd_kernel.cpp` 内 `runTAdd/LaunchTAdd/TADD(...)` 与文件名；`main.cpp` 内 `TADDTest`、`LaunchTAdd` 声明与 TEST_F 名；`gen_data.py` 内 `TAddParams/gen_golden_data_tadd/golden = input1 + input2` 与 case 名前缀；`CMakeLists.txt` 的 `pto_cpu_sim_st(tadd)` 参数。共四文件约 8 处。
5. 若本机未装 C++20 编译器或 numpy，输出会不同——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gen_data.py` 里 golden 要按 `[:row_valid, :col_valid]` 切片，而 input 不切？

**答案**：input.bin 必须覆盖 tile 的完整容量形状（TLOAD 按容量搬入，padding 语义另算），而指令只在有效区内有定义；golden 只对有效区计算正确答案，有效区外保持 `NumExt.zeros` 初始化。main.cpp 比对的是整个文件，若 golden 也全量计算，就会对「实现本不保证的有效区外区域」提出错误要求。

**练习 2**：新用例 `thypot` 忘记登记进 `ALL_TESTCASES`，`python3 tests/run_cpu.py -t thypot` 会发生什么？

**答案**：cmake 配置期 foreach 找不到名为 thypot 的列表项，不会 `add_subdirectory(thypot)`，于是构建产物里没有 thypot 可执行；run_cpu.py 在运行阶段找不到目标二进制而报错（或退化为无可运行用例）。即使目录和四件套都齐了，不登记就等于不存在——这也是守护脚本存在的意义。

**练习 3**：THYPOT 的 case 参数表为什么不应照抄 tadd 的 int32/int16 两个 case？

**答案**：`np.hypot` 对整型输入返回浮点结果，而 golden 的 dtype 必须与 C++ 侧 tile dtype 一致才能逐字节比对（`ResultCmp<T>` 按同 T 读两个文件）。勾股是浮点语义，用例应只覆盖 float32 与 float16（如 64x64 fp32、16x256 fp16），实现的 dtype 白名单也应只放行 float/half——测试设计与约束设计要同步收敛。

## 5. 综合实践

**任务：把 THYPOT 的 CPU 仿真部分完整做出来并跑通 ST 用例。** 请在你自己的克隆中操作（本讲义不改动仓库源码）。七步对应 4.1 清单：

**第 1 步：登记 Op 与流水线**（`include/pto/common/event.hpp`）

在 `Op` 枚举（L21 起）追加 `THYPOT,`；在 `PTO_DEFINE_OP_PIPE` 区（L176 附近）追加一行：

```cpp
PTO_DEFINE_OP_PIPE(Op::THYPOT, PIPE_V);  // 示例代码
```

**第 2 步：公共 API**（`include/pto/common/pto_instr.hpp`，仿照 L112-L118 的 TADD）

```cpp
// 示例代码
template <typename TileDataDst, typename TileDataSrc0, typename TileDataSrc1, typename... WaitEvents>
PTO_INST RecordEvent THYPOT(TileDataDst& dst, TileDataSrc0& src0, TileDataSrc1& src1, WaitEvents&... events)
{
    TSYNC(events...);
    MAP_INSTR_IMPL(THYPOT, dst, src0, src1);
    return {};
}
```

**第 3 步：CPU 实现**（新建 `include/pto/cpu/THypot.hpp`，镜像 [TAdd.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L19-L69) 的两层结构，仅列关键分支）

```cpp
// 示例代码：非分形 + 行主序分支（其余三分支照抄 TAdd_Impl 的 if constexpr 骨架）
template <typename tile_shape>
void THypot_Impl(
    typename tile_shape::TileDType dst, typename tile_shape::TileDType src0, typename tile_shape::TileDType src1,
    unsigned validRow, unsigned validCol)
{
    if constexpr (tile_shape::SFractal == SLayout::NoneBox) {
        if constexpr (tile_shape::isRowMajor) {
            cpu::parallel_for_rows(validRow, validCol, [&](std::size_t r) {
                const std::size_t base = r * tile_shape::Cols;
                PTO_CPU_VECTORIZE_LOOP
                for (std::size_t c = 0; c < validCol; ++c) {
                    const std::size_t idx = base + c;
                    dst[idx] = static_cast<typename tile_shape::DType>(   // double 中转，仿 OP_POW 范式
                        std::hypot(static_cast<double>(src0[idx]), static_cast<double>(src1[idx])));
                }
            });
        }
        // ... 列主序 / 分形分支同 TAdd_Impl
    }
}

template <typename tile_shape>
PTO_INTERNAL void THYPOT_IMPL(tile_shape& dst, tile_shape& src0, tile_shape& src1)
{
    // 此处可仿 npu/a2a3/TAdd.hpp 的 TAddCheck 补静态断言：
    // dtype 白名单（仅 float/half）+ src 有效区与 dst 一致
    THypot_Impl<tile_shape>(dst.data(), src0.data(), src1.data(), dst.GetValidRow(), dst.GetValidCol());
}
```

**第 4 步：装配**（`include/pto/common/pto_instr_impl.hpp` 的 `#ifdef __CPU_SIM` 块，L359-370 处）加一行 `#include "pto/cpu/THypot.hpp"`。

**第 5 步：ST 用例四件套**（`tests/cpu/st/testcase/thypot/`，以 4.3.3 的 tadd 为模板）：

- `thypot_kernel.cpp`：复制 tadd_kernel.cpp，L38 的 `TADD(...)` 换成 `THYPOT(dstTile, src0Tile, src1Tile)`；显式实例化只留两行——`float, 64, 64, 64, 64` 与 `aclFloat16, 16, 256, 16, 256`；
- `main.cpp`：全局替换 `TADDTest`→`THYPOTTest`、`LaunchTAdd`→`LaunchTHypot`，TEST_F 只留 `case_float_64x64_64x64_64x64` 与 `case_half_16x256_16x256_16x256` 两个；
- `gen_data.py`：golden 一行改为 `golden[:row_valid,:col_valid] = np.hypot(input1, input2)[:row_valid,:col_valid]`（输入保持 randint 1..10 转 float32/float16），case 参数表只留 fp32/fp16 两项，case 名前缀改 `THYPOTTest`；
- `CMakeLists.txt`：一行 `pto_cpu_sim_st(thypot)`。

**第 6 步：注册用例**：在 `tests/cpu/st/testcase/CMakeLists.txt` 的 `ALL_TESTCASES` 列表按字母序插入 `thypot`。

**第 7 步：运行验证**：

```bash
python3 tests/run_cpu.py -t thypot --verbose
```

**预期结果**：gtest 报告 2 个 case 通过（fp32 与 fp16 各一），`ResultCmp` 容差 0.001 内全对——勾股值域 [1.41, 14.14)，fp16 舍入误差远小于容差。**待本地验证**（本讲义编写环境未实际执行）。通过后自查：`python3 tests/validate_testcase_names.py` 无告警；`include/README.md` 状态表补上 THYPOT 行（CPU=Yes，其余 TODO）；`docs/isa/THYPOT.md` 与索引补齐。

**常见故障速查**：

| 症状 | 多半漏了 |
|---|---|
| 编译报 `THYPOT_IMPL` 未声明/未定义 | 第 4 步装配 include |
| `Event<THYPOT, ...>` 真机编译断言 invalid pipe | 第 1 步 `PTO_DEFINE_OP_PIPE` |
| main 报读不到 `input1.bin` | 三方命名不一致（TEST_F 名 vs gen_data 目录名） |
| `-t thypot` 找不到目标 | 第 6 步 ALL_TESTCASES 注册 |
| golden 比对 shape 不符 | 用例带了整型 dtype（见 4.3.5 练习 3） |

## 6. 本讲小结

- 一条新指令 = **8 处必改**（API、Op 枚举+流水线登记、CPU 实现、装配挂载、ISA 文档、文档索引、ST 四件套、状态表）**+ 2 处可选**（NPU 实现、CostModel 系数），横切接口/实现/装配/文档/测试五层。
- 三层命名严格一致（`THYPOT` / `THYPOT_IMPL` / 文件名）+ 汇总头按「架构 × 后端」宏挂载，是新指令被三套后端自动路由的全部机制。
- `PTO_DEFINE_OP_PIPE` 漏登记在 CPU 仿真上完全不可见，只在真机用 `Event<SrcOp,DstOp>` / `TSYNC<OpCode>()` 时编译期爆炸——清单纪律比测试更能兜住这类问题。
- ISA 文档的 Constraints 段必须与实现里的 `static_assert`/`PTO_ASSERT` 一一对应，且按架构分节；Math 段必须写明 valid region 定义域。
- ST 闭环的核心是三方命名契约（TEST_F 名 / gen_data 目录名 / main 寻址路径）与「实现-golden 成对修改」纪律；`ALL_TESTCASES` 注册 + 两个守护脚本防假覆盖。
- NPU 侧实现前先回答「有没有 1:1 intrinsic」：有则走 `TBinOp` 策略模板，无则组合实现并给公共 API 加 tmp 操作数（TFMOD 先例）。

## 7. 下一步学习建议

1. **补齐 NPU 侧拼图**：阅读 [include/pto/npu/a2a3/TFmod.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFmod.hpp) 与 [include/pto/npu/a2a3/TBinOp.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp)，为 THYPOT 的 A2/A3 组合实现（`vmul`+`vadd`+`vsqrt` 路线，**具体 intrinsic 组合待确认**）写出设计稿，并同步修改公共 API 增加 tmp。
2. **进入下一讲 u11-l2「架构适配：A5/A6 后端与跨代迁移」**：把 THYPOT 分别落到 `include/pto/npu/a5/` 与（若评审分配）`include/pto/npu/a6/`，体会同一指令在多代架构上的目录组织与取舍。
3. **走一遍真实贡献流程**：按 `CONTRIBUTING.md` 在 gitcode 上开 `Requirement|需求建议` Issue 描述 THYPOT 的背景/价值/设计，装好 `pre-commit`（`pip install pre-commit && pre-commit install`），用 `clang-format -i -style=file` 与 `ruff format` 过格式，再提 PR 并在 Issue 里回复 PR 链接请求评审。
4. **延伸阅读**：`tests/npu/a2a3/src/st/testcase/` 下同构的 NPU 树用例（`pto_vec_st` 注册），把你的 CPU 用例镜像一份到 NPU 树，为真机验证做准备。
