# 校验基类与通用检查机制

## 1. 本讲目标

本讲进入 asc-tools 的「校验引擎」——`api_check` 模块。学完后你应当能够：

- 说清 `api_check` 校验框架的**三层分层设计**（入口函数层 → 校验器子类层 → 基类与通用函数层），并理解它和 u3-l3 中「npuchk 类 stub」的衔接关系。
- 掌握基类 `TikcppBaseCheck` 提供的一组**通用校验方法**：Tensor scope（存储位置）检查、buffer 容量溢出检查、mask 合法性检查、地址对齐检查，以及围绕向量指令的越界（overflow）数学模型。
- 理解 `ASCENDC_CHECK` / `ASCENDC_CHECK_AND_LOG` 等**错误报告宏**如何用「失败即 `return false`」的方式自底向上传播违例，并用 `CHECK_LOG_ERROR` 把错误同时打到 `*_npuchk.log` 与 dlog 系统。

本讲是 u4 单元（API 校验框架）的总览与地基，后续 u4-l2（DataCopy 搬运校验）、u4-l3（向量计算校验）都是在基类之上做「特定 API 的参数校验」。

## 2. 前置知识

### 2.1 什么是 api_check、它何时被触发

在 u2-l1 我们知道：CPU Debug 模式下，同一份 Ascend C 源码里的内建函数（`Add`、`DataCopy` 等）会被转义成 CPU 上的可执行 stub。在 u3-l3 中我们又把这些 stub 分成三类：

- `AscendC` 前缀：**功能实现**（真正在 CPU 上算出结果）。
- `cceprint` 前缀：**打印跟踪**。
- `npuchk` 前缀：**运行时校验**——这就是 `api_check` 模块的入口。

也就是说，每当算子在 CPU 域执行一条内建指令，`npuchk` 类 stub 都会**同步**调用 `api_check` 的入口函数，检查这条指令的参数（Tensor 大小、地址、scope、mask、stride……）是否合法。违例会被记录到 `*_npuchk.log`，这正是 u5（NPU Check 工具）要解析的内容。因此一句话定位 `api_check`：**它是 npuchk 的检查内核，负责在 CPU 域运行算子时「边跑边查」参数合法性。**

### 2.2 NPU 向量指令的几个关键概念

`api_check` 大量校验都是围绕 NPU 向量指令的参数模型展开的，这里先建立直觉（细节在第 4.3 节用数学公式展开）：

- **repeat（重复）**：一条向量指令可以循环执行多次，每次处理一个「repeat」的数据。
- **mask（掩码）**：控制每个 repeat 里**哪些元素真正参与计算**。NPU 有两种解释方式：
  - **normal mode（位掩码）**：mask 是一个位图，第 *i* 位为 1 表示第 *i* 个元素生效，128 位最多控制 128 个元素。
  - **counter mode（计数）**：mask 直接表示「要算多少个元素」，只用低 32 位。
- **stride（步进）**：相邻 repeat 之间、相邻 block 之间地址如何跳跃，分别是 `repStride`、`blkStride`。
- **scope（存储位置）**：Tensor 物理上放在哪块硬件存储上——GM（全局显存）、UB（Unified Buffer）、L1、L0A/L0B/L0C 等。某些指令要求操作数必须在特定 scope。

### 2.3 C++ 继承与「通用方法下沉」

如果你熟悉 C++，`api_check` 的设计就是一个经典模板方法：把所有 API 都用得上的通用检查（越界、scope、对齐、mask）**下沉到一个基类**，各 API 的特殊检查由**子类**实现，子类复用基类方法。即使你不熟悉继承，只要记住「子类自动拥有基类的所有方法，可以直接调用」即可读懂本讲。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [cpudebug/src/api_check/inc/kernel_base_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h) | 基类 `TikcppBaseCheck` 与通用工具函数（`CheckTensorSizeOverflow`、`GetMaskLength` 等）的声明，定义 `ModeType`、`TensorOverflowParams`。 |
| [cpudebug/src/api_check/kernel_base_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp) | 上述声明的实现：通用检查方法、越界数学模型、mask/scope/对齐逻辑。**本讲的主战场。** |
| [cpudebug/src/api_check/kernel_check_params.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h) | 错误报告宏 `ASCENDC_CHECK` / `ASCENDC_CHECK_AND_LOG` / `CHECK_LOG_*`、`GlobalParams`、`CommonParams` 等基础设施。 |
| [cpudebug/src/api_check/kernel_check_util.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp) | **入口函数层**：`CheckFuncDataCopyImpl`、`CheckFuncVecBinaryImpl` 等一系列 `CheckFuncXxxImpl`，由 npuchk stub 调用，内部构造子类校验器实例。 |
| [cpudebug/utils/include/utils/kernel_utils_constants.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h) | 关键常量：`DEFAULT_BLOCK_SIZE=256`、`FULL_MASK`、`CONST_MASK_VALUE`、`DEFAULT_BLK_STRIDE`、`DEFAULT_REPEAT_STRIDE`、`ONE_REPEAT_BYTE_SIZE` 等。 |
| [cpudebug/src/api_check/inc/kernel_data_copy_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h) 与 [kernel_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp) | 一个具体子类 `TikcppDataCopyCheck`，用来演示「子类继承基类、复用通用检查」的写法。 |
| [tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp) | DataCopy 校验的单元测试，演示如何构造参数并触发检查（实践环节依据）。 |

> 阅读建议：先看 4.1 建立三层全景，再带着全景去读 4.2/4.3 的基类方法，最后用 4.4 的宏把「出错如何传播」补上。

## 4. 核心概念与源码讲解

### 4.1 api_check 校验框架的分层设计与 TikcppBaseCheck 基类

#### 4.1.1 概念说明

`api_check` 把「检查一条内建指令是否合法」这件事拆成三层，自底向上是：

1. **基类与通用函数层（kernel_base_check.\*）**：提供所有 API 都可能复用的通用检查——越界、scope、mask、对齐。本讲的全部内容都在这一层。
2. **校验器子类层（kernel_xxx_check.\*）**：每个子类（如 `TikcppDataCopyCheck`、`TikcppVecBinaryCheck`）针对一种 API，把该 API 特有的参数校验与若干基类通用检查**串成一条检查流水线**。
3. **入口函数层（kernel_check_util.cpp）**：一组 `CheckFuncXxxImpl` 自由函数，是 npuchk stub 调用的入口；它构造对应的子类实例，调用其 `CheckAllHighLevel()` / `CheckAllLowLevel()`。

为什么要分三层？因为校验逻辑里**「通用部分」远多于「特有部分」**：几乎所有向量指令都要查越界、都要查 scope、都要处理 mask。把这些下沉到基类，新增一个 API 的校验器时只需写「这个 API 特有的几何关系」，再拼装几个基类方法即可，避免重复造轮子。

#### 4.1.2 核心流程

下面是一次校验的端到端流程（以 `DataCopy` 为例）：

```text
npuchk 类 stub（构建期由 write_npuchk.py 生成，见 u3-l3）
        │  传入 chkParams + intriName（内建函数名，如 "mov_align"）
        ▼
CheckFuncDataCopyImpl(chkParams, intriName)        ← 入口函数层
        │  构造 check::TikcppDataCopyCheck chkIns{intriName, chkParams}
        ▼
chkIns.CheckAllHighLevel()                          ← 校验器子类层
        │  ASCENDC_CHECK(CheckAddrAlign())
        │       └─ CheckDataCopyAlign() → CheckTensorAddrAlign(...)   ← 调用基类通用方法
        ▼
TikcppBaseCheck::CheckTensorAddrAlign(...)          ← 基类层
        │  ASCENDC_CHECK_AND_LOG(...) 失败则 return false
        ▼
返回 bool：true=通过，false=有违例（npuchk 层据此记录到 *_npuchk.log）
```

关键点：**控制流通过返回值自底向上传播**。任何一层返回 `false`，上层用 `ASCENDC_CHECK(...)` 捕获后会立刻 `return false`，整条流水线短路退出（详见 4.4）。

#### 4.1.3 源码精读

先看基类 `TikcppBaseCheck` 的骨架。它非常「轻」——几乎只持有一个 `apiName`，真正的逻辑都是不带状态的成员函数：

> [kernel_base_check.h:78-82](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L78-L82) —— 基类构造函数只接收一个 `name`（即内建函数名 `intriName`），存入 `apiName`，虚析构函数留作安全基类。

`apiName` 的作用贯穿全篇：所有错误信息里都会带上它（如 `"Failed to check %s size in %s..."` 中的 `%s` 就是 `apiName`），让你一眼看出是哪条指令报的错。

再看入口函数层的典型写法：

> [kernel_check_util.cpp:61-69](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L61-L69) —— `CheckFuncDataCopyImpl` 是 npuchk stub 的入口：先用宏校验 `intriName` 非空，再用 `{intriName, chkParams}` 构造子类实例，调用 `CheckAllHighLevel()`。

其中 `ASCENDC_CHECK_INTRI_NAME` 是入口层的统一防御性检查：

> [kernel_check_util.cpp:23-29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L23-L29) —— 若 `intriName` 为空指针或空串，记一条 `CHECK_LOG_ERROR` 并 `return false`。

最后看子类如何继承基类：

> [kernel_data_copy_check.h:22-32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h#L22-L32) —— `class TikcppDataCopyCheck : public TikcppBaseCheck`。构造时通过 `TikcppBaseCheck(name)` 把 `apiName` 交给基类，自己只额外保存一个 `param_`（DataCopy 专属参数）。

这就体现了「特有参数归子类，通用能力归基类」的分工。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证三层结构，能在代码里指出每一层的位置。

**操作步骤**（源码阅读型实践）：

1. 打开 [kernel_check_util.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp)，找到 `CheckFuncDataCopyImpl`（第 61 行），确认它构造的是 `check::TikcppDataCopyCheck`。
2. 打开 [kernel_data_copy_check.cpp:21-25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L21-L25)，确认 `CheckAllHighLevel()` 内部只调用 `CheckAddrAlign()`（子类自己的方法）。
3. 跟到 [kernel_data_copy_check.cpp:27-50](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L27-L50)，看 `CheckDataCopyAlign` 又调用了基类方法 `CheckTensorAddrAlign`。
4. 打开 [kernel_base_check.h:78-82](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L78-L82)，确认 `CheckTensorAddrAlign` 声明在基类 `TikcppBaseCheck` 中。

**需要观察的现象**：调用链从入口函数 → 子类 → 基类逐层下沉；`apiName`（`"DataCopy"`/`"mov_align"`）一路透传到底层错误信息。

**预期结果**：你能画出 4.1.2 的调用链，并指明每个方法分别属于哪一层。

#### 4.1.5 小练习与答案

**练习 1**：入口函数层为什么设计成「自由函数 `CheckFuncXxxImpl`」而不是直接让 npuchk stub 调用子类的 `CheckAllHighLevel()`？

**参考答案**：自由函数层起到「门面（facade）」作用——它统一处理 `intriName` 非空检查（`ASCENDC_CHECK_INTRI_NAME`）、统一构造校验器实例、统一对外签名（`bool ...(Params&, const char*)`）。这样 npuchk stub（构建期生成、数量庞大）只需调用一个稳定、简单的 C 风格入口，不必关心 C++ 子类的存在，也降低了生成代码的复杂度。

**练习 2**：`TikcppBaseCheck` 的构造函数参数 `name` 最终被用在哪些地方？

**参考答案**：存入成员 `apiName`（[kernel_base_check.h:174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L74)），随后出现在几乎所有 `CHECK_LOG_ERROR` 的错误串里（如 scope/对齐/越界错误中的 `"%s"`），用于指明是哪条内建指令报错。

---

### 4.2 通用检查方法：scope / buffer / mask / 地址对齐

#### 4.2.1 概念说明

基类 `TikcppBaseCheck` 提供了一组「与具体 API 无关」的通用检查，它们解决四类常见违例：

| 方法 | 检查什么 | 典型违例 |
|------|----------|----------|
| `CheckTensorScope` | Tensor 的存储位置是否满足指令要求 | 把本该放 UB 的操作数放到了 GM |
| `CheckBufferSizeOverFlow` | 申请/使用的 buffer 是否超过硬件容量上限 | 在 UB 上申请了超过 `UB_SIZE` 的空间 |
| `CheckMaskArray` / `CheckMaskImm` | mask 值是否在合法范围内 | counter 模式 mask 超过 32 位；normal 模式 mask 为 0 |
| `CheckTensorAddrAlign` | Tensor 起始地址是否满足对齐要求 | UB 操作数未 32 字节对齐 |

这些方法的共同特点是：**判定逻辑与具体 API 无关**，所以任何子类都能直接调用。

#### 4.2.2 核心流程

- **scope 检查**：把 Tensor 的「逻辑位置」（`TPosition`，如 `VECCALC`）经 `GetPhyType` 映射成「物理位置」（`Hardware`，如 `UB`），再与指令期望位置 `expectedPos` 比较；不等则报错。
- **buffer 检查**：直接比大小 `localSize > bufferSize`。
- **mask 检查**：分 counter / normal 两种模式分别判定（详见第 4.3 节对两种模式的解释）。
- **对齐检查**：用「Tensor 绝对地址 − 硬件基址」得到段内偏移，再看 `offset % alignBytes == 0`。

#### 4.2.3 源码精读

**scope 检查**——逻辑位置到物理位置的转换是关键一步：

> [kernel_base_check.cpp:124-146](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L124-L146) —— `CheckTensorScope`：先用 `GetPhyType` 把 `logicPos` 转成物理位置 `hardPos`；若该物理位置不在 `GlobalParams::hardwareNameMap` 中，记错并返回 `false`；否则用 `ASCENDC_CHECK_AND_LOG(hardPos == expectedPos, {...})` 比对，不等则打印「支持的位置 vs 当前位置」并返回 `false`。

注意这里的 `hardwareNameMap` 是一张「物理位置 → 名字（"GM"/"UB"/...）」的映射，定义在 [kernel_check_params.h:87-92](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L87-L92)，专门用于错误信息的可读化。

**buffer 容量检查**——最简单的一种，但它故意**不**用 `ASCENDC_CHECK_AND_LOG`：

> [kernel_base_check.cpp:148-158](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L148-L158) —— `CheckBufferSizeOverFlow`：直接 `if (localSize > bufferSize)` 判断，记 `CHECK_LOG_ERROR` 后 `return false`。硬件容量上限来自 `GlobalParams::bufferSizeMap`（[kernel_check_params.h:94-100](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L94-L100)，如 `UB→UB_SIZE`、`L1→L1_SIZE`）。

> 思考题（答案见 4.2.5）：为什么这个方法不像别的方法那样用 `ASCENDC_CHECK_AND_LOG`？

**mask 合法性检查**——分 counter / normal 两路：

> [kernel_base_check.cpp:186-208](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L186-L208) —— `CheckMaskImm`：counter 模式（`ModelFactoryGetMaskMode() == 1`）下，mask 表示元素个数，只用低 32 位，超过 `0xffffffff` 仅给 `CHECK_LOG_WARNING`（警告，不阻断）；normal 模式下，在 Ascend910/310p/610（`__NPU_ARCH__ == 1001 || 2002`）上不允许 mask 为 0，否则 `ASCENDC_CHECK_AND_LOG` 报错。`CheckMaskArray`（[kernel_base_check.cpp:161-183](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L161-L183)）逻辑相同，只是 mask 拆成了 `[maskHigh, maskLow]` 两段。

注意一个重要差别：counter 模式越界只是 **warning**（不 `return false`），而 normal 模式的「mask 为 0」是 **error**（`return false`）。这反映了硬件语义——counter 超过 32 位时高位被截断，属「可运行但有风险」；而 normal 模式 mask 全 0 是「这条指令什么都不算」，在老架构上是真实错误。

**地址对齐检查**——把绝对地址换算成段内偏移再取模：

> [kernel_base_check.cpp:353-366](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L353-L366) —— `CheckTensorAddrAlign`：`hardwareBaseAddr` 来自 `ConstDefiner`（各硬件存储的基地址，见 u3-l1 的共享内存布局），`tensorAbsPos = tensorAddr - hardwareBaseAddr` 得到段内偏移；`ASCENDC_CHECK_AND_LOG((tensorAbsPos % alignBytes) == 0, {...})` 判对齐。

对齐字节数随硬件存储不同：GM 是 1 字节对齐（即不查），UB/L1 是 32 字节，L0A/L0B/L0C 是 512 字节相关，这套表在子类 [kernel_data_copy_check.cpp:30-39](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L30-L39) 中给出，子类再把 `alignBytes` 传给基类的 `CheckTensorAddrAlign`。

#### 4.2.4 代码实践

**实践目标**：通过阅读 UT，理解如何构造参数触发一次成功的 scope/对齐校验，以及一次失败的校验。

**操作步骤**（源码阅读 + 本地可选运行型实践）：

1. 打开 [test_data_copy_check.cpp:223-240](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp#L223-L240)，看 `TestDataCopyApiCheckSuite`。
2. 关注第 225-227 行的参数：`dstAddr=0x12345`、`srcAddr=0x6789`、`dstPos=GM`、`srcPos=UB`、`blockCount=1, blockLen=1, srcStride=8, dstStride=8`、`expect=false`。这是一个**预期失败**的用例。
3. 推断失败原因：`srcAddr=0x6789` 不是 32 的倍数，而 src 在 UB（要求 32 字节对齐），所以 `CheckTensorAddrAlign` 会失败。
4. （可选，待本地验证）按 u9-l3 介绍的方式编译并运行该 UT：`bash build.sh --utest`（具体命令以 build.sh 当前实现为准，待本地验证），观察 `[ERROR] ... should be 32 byte aligned` 之类的输出。

**需要观察的现象**：用一个未对齐的 UB 地址，`CheckFuncDataCopyImpl` 返回 `false`，并打印带 `[ERROR]` 前缀的对齐错误。

**预期结果**：`EXPECT_EQ(flag, param.expect)` 中 `flag==false`、`expect==false`，测试通过。

#### 4.2.5 小练习与答案

**练习 1（思考题答案）**：`CheckBufferSizeOverFlow` 为什么用 `if + return false` 而不用 `ASCENDC_CHECK_AND_LOG`？

**参考答案**：功能上两者等价（失败都 `return false`）。这里手写 `if` 主要是为了在记日志时能用 `errMsg` 拼出更具体的上下文（哪个 buffer、分配了多少、上限多少），写法上更直接。它和宏版本是同一套「失败即返回 false」语义的两种表达。

**练习 2**：counter 模式下 mask 超过 `0xffffffff` 时，为什么只给 warning 而不让校验失败？

**参考答案**：counter 模式下 mask 表示「要计算的元素个数」，硬件只用其低 32 位（`CONST_UINT32_MAX = 0xffffffff`），高位会被截断，指令仍能运行，只是用户写的值与实际生效的值不一致，属「有风险但非非法」，因此用 `CHECK_LOG_WARNING` 提示而非 `return false` 阻断。

---

### 4.3 越界检查的数学模型：mask 长度与向量 offset

#### 4.3.1 概念说明

越界检查（overflow check）是 `api_check` 最核心、也最数学化的一类检查，目标是回答：**一条向量指令真正访问到的最远地址，是否超出了 Tensor 已分配的大小？**

为此需要解决两个子问题：

1. **一个 repeat 里到底算了多少个元素？** 由 `GetMaskLength` 从 mask 推出。
2. **多个 repeat 叠加后，最远访问到第几个元素？** 由 `CalculateVectorMaxOffset` 用 stride 几何关系算出。

最后把「最远元素数 × 元素大小」得到需要的字节数 `expectedSize`，与 Tensor 实际大小 `tensorSize` 比较——这正是 `CheckTensorSizeOverflow` 的职责。理解这一节，你就理解了 u4-l3（向量计算校验）的全部数学基础。

#### 4.3.2 核心流程

**第一步：mask → 每个 repeat 的元素数 `maskLen`（GetMaskLength）**

mask 在代码里是一个长度为 1 或 2 的数组。长度 2 时是 `[maskHigh, maskLow]`（注意：下标 0 是 high、下标 1 是 low，见 `CommonParams` 的 `MASK_HIGH_IDX=0 / MASK_LOW_IDX=1`），合起来 128 位，第 *i* 位控制第 *i* 个元素。`GetMaskLength` 找的是**最高置位比特的位置**，即「从第 0 个元素起到最后一个生效元素」的个数：

\[ \text{maskLen} = \begin{cases} 64 - i_{\text{low}}, & \text{maskHigh} = 0 \\ 128 - i_{\text{high}}, & \text{maskHigh} \ne 0 \end{cases} \]

其中 \(i\) 是从最高位（bit 63）向下扫描时第一个命中 1 的轮次。随后若 `dtypeSize >= 4`，还要用 `maxElePerRep = 256 / dtypeSize` 截断（因为一个 repeat 物理上最多 256 字节，例如 fp32 最多 64 个元素）。

**第二步：repeat/stride 几何 → 最远元素偏移（CalculateVectorMaxOffset）**

一条向量指令的数据布局是「repeat 内分 block，repeat 间跳 `repStride`，block 间跳 `blkStride`」。设 \(R\) 为 repeat 次数、\(s_r\) 为 repStride、\(s_b\) 为 blkStride、\(L_b\) 为每 block 的元素数、\(m\) 为 maskLen，则最远元素偏移（以元素为单位）：

\[ B_{\text{last}} = \lceil m / L_b \rceil \]（最后一个 repeat 需要多少个 block）
\[ e_{\text{last}} = \begin{cases} m \bmod L_b, & m \bmod L_b \ne 0 \\ L_b, & \text{otherwise} \end{cases} \]（最后一个 block 里用到的元素数）
\[ \text{maxOffset} = \left((R-1)\,s_r + (B_{\text{last}}-1)\,s_b\right) \cdot L_b + e_{\text{last}} \]

**第三步：比较 expectedSize 与 tensorSize（CheckTensorSizeOverflow）**

把 `maxOffset` 换算成字节（× `dtypeSize`）得到 `expectedSize`，与 `tensorSize` 比较：`expectedSize <= tensorSize` 则通过，否则报错。这里出现的 `ModeType` 就是用来在错误信息里标注「这次越界发生在 normal mode 还是 counter mode」。

#### 4.3.3 源码精读

**GetMaskLength 实现**——逐位从高到低扫描：

> [kernel_base_check.cpp:25-51](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L25-L51) —— `maskArray[MASK_HIGH_IDX] & (CONST_MASK_VALUE >> i)` 中，`CONST_MASK_VALUE = 0x8000000000000000`（最高位，见 [kernel_utils_constants.h:25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h#L25)），右移 *i* 位即检查 bit(63−i)；第一个命中的 *i* 就是最高置位比特，`maskLen = 64 − i`（low）或 `128 − i`（high）。最后用 `maxElePerRep = DEFAULT_BLOCK_SIZE / dtypeSize`（`DEFAULT_BLOCK_SIZE=256`，[kernel_utils_constants.h:28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h#L28)）对大 dtype 截断。

**CalculateVectorMaxOffset 实现**——上面公式的直接翻译：

> [kernel_base_check.cpp:228-240](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L228-L240) —— 注意 `repeatTimes == 0` 直接返回 0（什么都不算）；`DivCeil(maskLen, blockLen)` 即 \(B_{\text{last}}\)；最后一行把「repeat 跳跃 + block 跳跃」换算成元素数并加上最后一个 block 的有效元素 `eleNumLastBlk`。

`CalculateNeededTensorSize`（[kernel_base_check.cpp:244-260](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L244-L260)）在此基础上把元素数乘 `dtypeBytes` 得到字节数，并特判 int4（每字节存 2 个元素，`INT4_TWO=2`，[kernel_utils_constants.h:36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h#L36)）。

**CheckTensorSizeOverflow 实现**——本讲的「主角函数」，也是配套实践任务的对象：

> [kernel_base_check.cpp:53-70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L53-L70) —— 先据 `mode` 拼一个后缀串 `curMode`（`" when in normal mode"` / `" when in counter mode"` / 空串），再用 `ASCENDC_CHECK_AND_LOG(expectedSize <= tensorSize, {...})` 比大小：比较失败时执行花括号里的 `CHECK_LOG_ERROR`（打印 tensor 名、API 名、模式、需要的字节数、实际字节数），然后 `return false`；比较通过则落到函数末尾 `return true`。

注意它是一个**自由函数**（声明在 `check` 命名空间里、但不在 `TikcppBaseCheck` 类内，见 [kernel_base_check.h:66-68](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L66-L68)），这样无论是基类成员（如 `CheckTensorOverflowLowNorm`）还是子类成员（如 `kernel_vec_scatter_check.cpp` 的 scatter 校验）都能直接调用，不需要借助对象。

**ModeType 三种模式的区别**——枚举定义：

> [kernel_base_check.h:28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L28) —— `enum class ModeType : uint8_t { NONE_MODE = 0, NORM_MODE = 1, COUNTER_MODE = 2 };`

三种模式**只影响错误信息的后缀文字，不影响比较逻辑**（比较永远是 `expectedSize <= tensorSize`）。它们的真正用途是告诉用户「这次越界是在哪种 mask 解释下发生的」：

| 枚举值 | 数值 | 后缀 | 语义 |
|--------|------|------|------|
| `NONE_MODE` | 0 | （空） | 不区分模式（如搬运类、对齐类检查） |
| `NORM_MODE` | 1 | ` when in normal mode` | mask 按位掩码解释时越界 |
| `COUNTER_MODE` | 2 | ` when in counter mode` | mask 按元素个数解释时越界 |

各调用点按自己指令所处的模式传入对应枚举。例如基类内部 `CheckTensorOverflowLowNorm` 传 `NORM_MODE`、`CheckTensorOverflowLowCounter` 传 `COUNTER_MODE`（[kernel_base_check.cpp:285 与 296](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L285-L296)），子类 `kernel_vec_reduce_other_whl_check.cpp:54/69` 也分别传 `COUNTER_MODE`/`NORM_MODE`。

**子类如何复用这套数学模型**——一个真实调用点：

> [kernel_vec_scatter_check.cpp:34-35](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_scatter_check.cpp#L34-L35) —— Scatter 校验先把地址偏移换算成字节得到 `maxOffset`，再 `ASCENDC_CHECK(CheckTensorSizeOverflow(maxOffset, param_.dstSize, "dstLocal", "Scatter"))`。同一个 scatter 校验函数随后还链式调用了 `CheckTensorScope`、`CheckBufferSizeOverFlow`、`CheckTensorOverflowLow/High` 等基类方法（见 4.2 节引用），完整展示了「子类拼装基类通用检查」的模式。

> 补充：counter 模式需要先把「按个数算的 mask」拆成「main block（满 repeat）+ tail block（余数）」两组 norm-mode 计算，再取两者最远端的最大值，这套逻辑在 `CounterSplitMainTail`（[kernel_base_check.cpp:211-222](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L211-L222)）与 `CheckTensorOverflowLowCounter`（[kernel_base_check.cpp:264-287](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L264-L287)）中实现，理解了本节的 norm 模型后再看这部分会非常自然。

#### 4.3.4 代码实践（对应配套实践任务）

**实践目标**：在 `kernel_base_check.cpp` 中找到 `CheckTensorSizeOverflow`，说清 `expectedSize/tensorSize` 比较失败时如何上报，以及 `ModeType` 三种模式的区别。

**操作步骤**（源码阅读型实践，必做；本地运行型为可选）：

1. 打开 [kernel_base_check.cpp:53-70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L53-L70)。
2. **比较如何上报**：跟踪 `ASCENDC_CHECK_AND_LOG(expectedSize <= tensorSize, { CHECK_LOG_ERROR(...); })` 的展开（见 4.4 节宏定义）。当 `expectedSize > tensorSize`（即 `!(expectedSize <= tensorSize)`）时，先执行花括号里的 `CHECK_LOG_ERROR` 打印一行 `[ERROR] Failed to check <tensor> size in <api><mode>, tensor size needs to be at least <expected> bytes, while current tensor size is only <actual> bytes.`，随后宏执行 `return false`，`CheckTensorSizeOverflow` 直接返回 `false`；调用方再用 `ASCENDC_CHECK(CheckTensorSizeOverflow(...))` 把 `false` 继续向上传播。
3. **三种模式区别**：对照上表填写——`NONE_MODE` 无后缀（搬运/对齐类）、`NORM_MODE` 加 ` when in normal mode`（位掩码）、`COUNTER_MODE` 加 ` when in counter mode`（元素计数）。强调：**比较逻辑与模式无关**，模式只决定错误文字。
4. （可选，待本地验证）在 [test_vec_reduce_other_whl_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_vec_reduce_other_whl_check.cpp) 中找一个 `expect=false` 的用例，按 u9-l3 的方式运行 UT，观察终端里带 `when in counter mode` / `when in normal mode` 后缀的 `[ERROR]` 行。

**需要观察的现象**：构造一个「需要的字节 > Tensor 实际大小」的情形，运行后能在终端看到 `[ERROR]` 行，且后缀与该指令的 mask 模式一致。

**预期结果**：你能在不运行代码的前提下，准确预测某条指令越界时错误信息的文字（含后缀）；本地运行（如能编译）则应看到对应 `[ERROR]` 输出，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：给定 `repeatTimes=3, repStride=8, blkStride=1, blockLen=8(元素), maskLen=8(元素)`，手算 `CalculateVectorMaxOffset`。

**参考答案**：\(B_{\text{last}}=\lceil 8/8\rceil=1\)；\(e_{\text{last}}=8\)（因为 \(8\bmod 8=0\)，取 \(L_b=8\)）；\(\text{maxOffset}=((3-1)\times8+(1-1)\times1)\times8+8=(16+0)\times8+8=136\) 个元素。

**练习 2**：为什么 `CheckTensorSizeOverflow` 被设计成命名空间内的自由函数，而不是 `TikcppBaseCheck` 的成员函数？

**参考答案**：因为它是一个无状态的纯比较工具（两个大小 + 名字 + 模式 → bool），不依赖任何成员变量；做成自由函数后，既能被基类成员（`CheckTensorOverflowLowNorm` 等）调用，也能被子类成员（如 scatter 校验）直接调用，不必通过对象、也不必每个子类重复声明。

**练习 3**：一个 fp32 的向量指令，mask 写成了 128 位全 1，`GetMaskLength` 最终返回多少？为什么？

**参考答案**：先按位扫描得 `maskLen=128`，但因 `dtypeSize(fp32)=4 >= sizeof(uint32_t)=4`，触发截断 `maxElePerRep = DEFAULT_BLOCK_SIZE / dtypeSize = 256/4 = 64`，最终返回 `64`。即一个 repeat 最多处理 64 个 fp32（256 字节）。

---

### 4.4 错误报告机制：ASCENDC_CHECK 宏族与日志

#### 4.4.1 概念说明

`api_check` 的错误报告建立在一组小宏之上，它们统一了「检查失败时如何记录、如何退出」。理解这组宏是读懂所有 `_check.cpp` 文件的前提——你会看到几乎每一行业务校验都被 `ASCENDC_CHECK(...)` 或 `ASCENDC_CHECK_AND_LOG(...)` 包裹。

核心宏有两个：

- `ASCENDC_CHECK(x)`：求值 `x`（通常是一个返回 `bool` 的检查调用），若为 `false` 则 `return false`，**不打日志**。
- `ASCENDC_CHECK_AND_LOG(cond, behavior)`：若 `cond` 为假，则执行 `behavior`（通常是一句 `CHECK_LOG_ERROR(...)`）再 `return false`。

外加四个日志宏 `CHECK_LOG_DEBUG/INFO/WARNING/ERROR`，底层是 CANN 的 `dlog_*`；其中 `CHECK_LOG_ERROR` 额外 `printf("[ERROR]"...)`，使得错误同时出现在终端（进而被捕获进 `*_npuchk.log`）和 dlog 系统。

#### 4.4.2 核心流程

```text
某条检查语句 ASCENDC_CHECK_AND_LOG(cond, { CHECK_LOG_ERROR(...); })
        │
        ├── cond 为真 → 什么也不做，继续往下执行
        └── cond 为假 → ① 执行 CHECK_LOG_ERROR（printf + dlog_error）
                        ② return false（从当前函数退出）
                            │
                            ▼ 上层调用者通常也用 ASCENDC_CHECK(...) 包裹
                        逐层 return false，直到入口函数 CheckFuncXxxImpl 返回 false
                            │
                            ▼
                        npuchk 层据 false 记录一条违例（最终写入 *_npuchk.log）
```

两个关键设计：**（1）短路传播**——任一检查失败立即逐层 `return false`，整条流水线不再继续；（2）**错误信息双通道**——`[ERROR]` 既进 stdout/npuchk.log，又进 dlog，方便不同场景排查。

#### 4.4.3 源码精读

**两个核心宏**——注意它们都用 `do { ... } while (0)` 包裹，是标准的「宏语句」写法：

> [kernel_check_params.h:39-52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L39-L52) —— `ASCENDC_CHECK(x)`：`if (!(x)) { return false; }`；`ASCENDC_CHECK_AND_LOG(cond, behavior)`：`if (!(cond)) { behavior; return false; }`。二者的 `return false` 都是从**当前所在函数**返回，这是整条链路短路退出的物理基础。

**日志宏族**——`CHECK_LOG_ERROR` 的双通道是关键：

> [kernel_check_params.h:61-75](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L61-L75) —— `CHECK_LOG_INFO/WARNING` 只调 `dlog_info/dlog_warn`；`CHECK_LOG_ERROR` 先 `printf("[ERROR]" format "\n", ...)` 再 `dlog_error(...)`。模块名常量 `ASCENDC_MODULE_NAME` 固定为 `ASCENDCKERNEL`（[kernel_check_params.h:54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L54)）。

**配套基础设施**：`GlobalParams`（单例）持有 `hardwareNameMap`（位置→名字，供 scope 错误排版）与 `bufferSizeMap`（硬件容量上限，供 `CheckBufferSizeOverFlow` 取值）；`CommonParams` 给出 mask 相关下标与长度（`MASK_MAX_ELE_LEN=64`、`MASK_HIGH_IDX=0`、`MASK_LOW_IDX=1`）。

> [kernel_check_params.h:79-105](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L79-L105) 与 [kernel_check_params.h:126-130](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L126-L130)。

**一个完整调用示例**——回到 4.1 的 `CheckTensorSizeOverflow`，它的 `ASCENDC_CHECK_AND_LOG(expectedSize <= tensorSize, { CHECK_LOG_ERROR(...); })` 就是这两个宏最典型的搭配：条件是大小比较，behavior 是带完整上下文的错误日志。再看调用方 `ASCENDC_CHECK(CheckTensorSizeOverflow(...))`（如 [kernel_base_check.cpp:80](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L80) 的 `CheckTensorOverflowHigh`），用 `ASCENDC_CHECK` 透传返回值——内层已经打过日志，外层就无需再打。

#### 4.4.4 代码实践

**实践目标**：亲手展开一次宏，验证「失败即 `return false`」的短路传播。

**操作步骤**（源码阅读型实践）：

1. 打开 [kernel_base_check.cpp:80](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L80)：`ASCENDC_CHECK(CheckTensorSizeOverflow(needSize, bufferSize, tensorName, apiName));`
2. 按 [kernel_check_params.h:39-44](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L39-L44) 展开为：`do { if (!(CheckTensorSizeOverflow(needSize, bufferSize, tensorName, apiName))) { return false; } } while (0);`
3. 进一步假设内层 `CheckTensorSizeOverflow` 命中越界、已 `return false`，那么这里 `!(false)==true`，于是外层也 `return false`，`CheckTensorOverflowHigh` 直接退出。
4. 用一张纸画出从 `CheckTensorOverflowHigh` → `CheckTensorSizeOverflow` → `CHECK_LOG_ERROR` 的展开后的 C++ 代码。

**需要观察的现象**：宏展开后，逻辑等价于两层嵌套的 `if (!cond) return false;`；错误日志只在内层打印一次。

**预期结果**：你能口述「一次越界违例如何从最底层比较，经两层 `return false`，最终让入口函数返回 `false` 给 npuchk 层」。

#### 4.4.5 小练习与答案

**练习 1**：`ASCENDC_CHECK(x)` 和 `ASCENDC_CHECK_AND_LOG(cond, behavior)` 的区别是什么？什么时候用哪个？

**参考答案**：前者只判定 `x` 真假、失败即 `return false`，不打日志；后者失败时先执行 `behavior`（通常是日志）再 `return false`。当被检查的子函数**自己已经打过日志**时（如 `CheckTensorSizeOverflow`），外层用 `ASCENDC_CHECK` 透传即可，避免重复打日志；当这是一个**叶子判定**（没有更内层的日志）时，用 `ASCENDC_CHECK_AND_LOG` 在原地补上错误信息。

**练习 2**：为什么所有日志宏都用 `do { ... } while (0)` 包裹？

**参考答案**：为了让宏在语法上表现得像一条独立语句，避免 `if/else` 悬空（dangling-else）等经典宏陷阱。例如 `if (cond) ASCENDC_CHECK(...); else ...;` 若宏不包 `do/while` 会被错误解析。

---

## 5. 综合实践

**任务**：以「跟踪一条 DataCopy 指令的校验全链路」为主线，把本讲四个模块串起来，亲手定位一次违例。

**背景**：假设你在 CPU 域运行一个算子，终端打印了一行 `[ERROR] Failed to check dst tensor address alignment in DataCopy, current tensor address is 16585, which should be 32 byte aligned.`

**要求**：

1. **定位错误来源**：根据错误文字，判断它来自 4.2 节的哪个基类方法（提示：`CheckTensorAddrAlign`，位于 [kernel_base_check.cpp:353-366](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L353-L366)）。注意 `16585 = 0x6789`，正好是 [test_data_copy_check.cpp:226](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp#L226) 里 `srcAddr` 的值——确认这条错误就是那个预期失败的 UT 用例触发的。
2. **还原调用链**：按 4.1.2 的三层结构，从入口 `CheckFuncDataCopyImpl`（[kernel_check_util.cpp:61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L61)）→ 子类 `CheckAllHighLevel/CheckAddrAlign/CheckDataCopyAlign`（[kernel_data_copy_check.cpp:21-56](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L21-L56)）→ 基类 `CheckTensorAddrAlign`，画出调用图。
3. **解释传播**：用 4.4 节的宏展开，说明这次 `ASCENDC_CHECK_AND_LOG((tensorAbsPos % alignBytes) == 0, {...})` 失败后，`return false` 是如何逐层冒泡，最终让 UT 中 `EXPECT_EQ(flag, false)` 成立的。
4. **延伸（可选）**：把同一个用例的 `srcAddr` 改成 32 的倍数（如 `0x6780`），预测 `CheckTensorAddrAlign` 是否还会失败、`expect` 是否应改成 `true`。说明理由（提示：还需看 `srcPos=UB` 是否仍满足对齐）。

**预期产出**：一张调用链图 + 一段用宏展开解释 `return false` 传播的文字 + 第 4 步的预测与理由。

> 如果无法本地编译运行，第 1-3 步可纯靠源码阅读完成；第 4 步标注「待本地验证」。

## 6. 本讲小结

- `api_check` 是 npuchk 的**检查内核**，采用**三层结构**：入口函数层（`CheckFuncXxxImpl`）→ 校验器子类层（`TikcppXxxCheck`）→ 基类与通用函数层（`TikcppBaseCheck` + 自由函数）。
- 基类 `TikcppBaseCheck` 提供**与 API 无关的通用检查**：`CheckTensorScope`（存储位置）、`CheckBufferSizeOverFlow`（硬件容量上限）、`CheckMaskArray/CheckMaskImm`（mask 合法性）、`CheckTensorAddrAlign`（地址对齐）。
- 越界检查是一套**数学模型**：`GetMaskLength` 从 mask 算每 repeat 元素数，`CalculateVectorMaxOffset` 用 repeat/stride 几何算最远元素偏移，最后 `CheckTensorSizeOverflow` 比较 `expectedSize <= tensorSize`。
- `ModeType` 三种模式（`NONE/NORM/COUNTER`）**只影响错误信息后缀、不影响比较逻辑**，用于标注违例发生在哪种 mask 解释下。
- 错误报告由 `ASCENDC_CHECK` / `ASCENDC_CHECK_AND_LOG` 宏族驱动，核心机制是「**失败即 `return false`**」的短路传播，配合 `CHECK_LOG_ERROR` 的「stdout + dlog」双通道输出。
- `CheckTensorSizeOverflow` 是自由函数而非成员，体现了「无状态工具下沉到命名空间、供各层复用」的设计取向。

## 7. 下一步学习建议

- **u4-l2（DataCopy 搬运类校验）**：本讲只看了 DataCopy 子类的对齐检查，下一讲会完整展开搬运类 API 的源/目的范围、对齐、scope 校验，是 4.2/4.3 在搬运场景的纵深应用。
- **u4-l3（向量计算类校验）**：直接承接本讲 4.3 的数学模型，深入 binary/reduce/broadcast 等校验器如何把 repeat/mask/stride 与 Tensor 容量挂钩，建议带着本讲的 `CalculateVectorMaxOffset` 公式去读。
- **u5-l1（npu check 错误体系）**：本讲产生的 `[ERROR]` 行最终进入 `*_npuchk.log`，u5 会讲这些错误如何被分类（`ErrorWrite/ErrorBuffer/...`）和解析，把「检查内核」与「错误产物」连成闭环。
- 若想立刻动手扩展，可跳到 **u10-l1（扩展 API 校验器）**，那里会教你在基类之上为一条新 API 编写校验器骨架。
