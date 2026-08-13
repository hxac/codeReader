# 向量计算类校验

## 1. 本讲目标

本讲是 API 校验框架（u4-l1 / u4-l2）的延续，聚焦**向量计算类** API 的校验器。

 Ascend C 的向量指令（`Add`、`Mul`、`Compare`、`ReduceSum`、`BroadCast` 等）和搬运类指令（`DataCopy`，见 u4-l2）的校验重点完全不同：搬运类关心「源/目的范围与对齐」，而向量类关心的是一组**指令参数几何**——`repeat`、`mask`、`stride` 如何共同决定一条指令到底会访问 Tensor 的哪些地址。

学完本讲你应当能够：

1. 用「block / repeat / mask / stride」这套语言描述一条向量指令的访存行为。
2. 读懂 `TikcppVecBinaryCheck`、`TikcppVecReduceCheck` 等校验器如何把这套参数翻译成「Tensor 最少需要多少字节」。
3. 理解 `GetMaskLength` 与 `CalculateVectorMaxOffset` 这两个数学函数如何串起 `mask` 与「Tensor 溢出判断」。

## 2. 前置知识

本讲假定你已经读过 u4-l1（校验基类与通用检查机制）和 u4-l2（搬运类校验）。我们直接复用其中的结论，不再重复：

- **三层结构**：入口函数层（`CheckFuncXxxImpl`）→ 校验器子类层（`TikcppXxxCheck`，继承自 `TikcppBaseCheck`）→ 基类与通用函数层。
- **控制流约定**：所有检查用 `ASCENDC_CHECK(x)` 宏包裹，`x` 为假即 `return false` 短路退出，违例最终记入 `*_npuchk.log`。
- **关键通用函数**（u4-l1 已讲）：`CheckTensorScope`（存储位置）、`CheckBufferSizeOverFlow`（硬件容量上限）、`CheckTensorAddrAlign`（地址对齐）、`CheckTensorSizeOverflow`（越界比较）。
- **两种 mask 模式**：normal（位掩码）与 counter（计数），由 `ModeType` 标识，仅影响错误信息后缀与少量分支，不影响「比较 expectedSize ≤ tensorSize」的本质。

如果你对这些概念已经陌生，建议先回看 u4-l1 的「越界检查数学模型」一节。

本讲新增的硬件术语，会在 4.1 节逐一解释。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [kernel_vec_binary_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp) | 二元向量计算（`Add`/`Sub`/`Mul`/`Compare` 等）的校验器实现，本讲的主线。 |
| [kernel_vec_binary_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_vec_binary_check.h) | `TikcppVecBinaryCheck` 类声明。 |
| [kernel_check_vec_binary_util.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_vec_binary_util.h) | 二元向量 API 的**参数容器** `VecBinaryApiParams` 与入口函数声明。 |
| [kernel_check_vec_reduce_util.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_vec_reduce_util.h) | 归约 API 的参数容器 `VecReduceApiParams` 与入口函数声明。 |

此外会引用两个 u4-l1 已覆盖、但本讲必须精读的基类文件：

| 文件 | 本讲用到部分 |
| --- | --- |
| [kernel_base_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp) | `GetMaskLength`、`CalculateVectorMaxOffset`、`CheckTensorOverflowLow` 等数学核心。 |
| [kernel_base_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h) | `TensorOverflowParams` 结构、`ModeType` 枚举。 |

## 4. 核心概念与源码讲解

### 4.1 向量指令参数模型：repeat / mask / stride

#### 4.1.1 概念说明

要把一条向量指令「算到哪、写到哪」说清楚，必须先理解 NPU 向量部件的寻址模型。这套模型由四个量组成：

- **block（块）**：向量部件寻址的最小单位，固定 **32 字节**为一个 block（`ONE_BLK_SIZE = 32`）。也就是说，即使你只想读 1 个 float，硬件也会按 32 字节对齐地取一整块。
- **repeat（重复）**：一条向量指令内部的最小计算循环。一个 repeat 固定处理 **8 个 block = 256 字节**（`ONE_REP_BYTE_SIZE = 256`，`BLK_NUM_PER_REP = 8`）。对 float32（4 字节）而言，一个 repeat 算 64 个元素；对 half（2 字节）算 128 个。
- **repeatTimes（重复次数）**：一条指令可把上述 repeat 重复执行最多 255 次（`MAX_REPEAT_TIMES = 255`），用更少的指令完成大批量计算。
- **mask（掩码）**：在每个 repeat 内，**真正参与计算的元素**由 mask 选定。两种解释方式见 4.2 节。
- **stride（步进）**：相邻 block 之间、相邻 repeat 之间的地址间隔，单位都是「block」：
  - `blkStride`：同一 repeat 内相邻 block 的间隔，默认 `1`（连续）。
  - `repStride`：相邻 repeat 的间隔，默认 `8`（正好接上一repeat，连续）。

一句话直觉：**一条向量指令 = 在 Tensor 上画出一个由 `repeatTimes × stride` 棱角勾勒的「访问脚印」，再用 `mask` 在每个 repeat 内裁出真正计算的元素。** 校验器的全部工作，就是把这只「脚印」的**最远端点**算出来，再去和 Tensor 容量比大小。

#### 4.1.2 核心流程

这些量是如何被收集进校验器的？答案是**参数容器**。二元向量 API 把用户传入的 `dst/src0/src1` 三元张量及其 `repeatTimes/blkStride/repStride` 打包成一个结构体，交给校验器：

```text
入口函数 CheckFuncVecBinaryImpl(...)
        │  构造 VecBinaryApiParams（填入地址、dtype、size、repeat、stride、pos）
        ▼
TikcppVecBinaryCheck chkIns{name, params}
        │
        ▼
chkIns.CheckAllLowLevel(maskArray)   或   chkIns.CheckAllHighLevel()
```

低层（low-level，带 `mask`）与高层（high-level，不带 `mask`，改用 `calCount`）走两条不同分支，详见 4.3 节。

#### 4.1.3 源码精读

`VecBinaryApiParams` 是二元校验的全部输入，它把上面四个量原样保存，注意 stride 的单位是 block：

[参数容器的关键字段](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_vec_binary_util.h#L81-L103) —— `repeatTimes`（uint8，即 ≤255）、`dstBlockStride/src0BlockStride/src1BlockStride`（block 为单位）、`dstRepeatStride/src0RepeatStride/src1RepeatStride`（block 为单位），以及三个张量的 `addr/dtypeBytes/size`。

这些字段的「默认值」由全局常量给出，理解默认值就理解了「连续访问」的几何：

[DEFAULT_BLK_STRIDE = 1, DEFAULT_REPEAT_STRIDE = 8](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_utils_constants.h#L22-L29) —— 块步进默认 1、repeat 步进默认 8（恰好等于一个 repeat 的 8 个 block，即前后两 repeat 地址首尾相接）。

平台级常量（每架构一份，此处以 ascend910B1 为例）定义了 block/repeat 的字节数：

[ONE_BLK_SIZE = 32, ONE_REP_BYTE_SIZE = 256, BLK_NUM_PER_REP = 8](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/ascend910B1_ini.h#L40-L43) —— 这三个数构成了向量指令访存模型的「坐标系」。

#### 4.1.4 代码实践

**实践目标**：建立「一个 repeat 等于多少元素」的直觉，作为 4.2 数学推导的准备。

**操作步骤**：

1. 打开 `ascend910B1_ini.h`，确认 `ONE_BLK_SIZE=32`、`ONE_REP_BYTE_SIZE=256`。
2. 对下表三种 dtype 手算「每 block 元素数」与「每 repeat 元素数」。

| dtype | dtypeBytes | 每 block 元素数 = 32 / bytes | 每 repeat 元素数 = 256 / bytes |
| --- | --- | --- | --- |
| float32 | 4 | 8 | 64 |
| half / bf16 | 2 | 16 | 128 |
| int8 | 1 | 32 | 256 |

**需要观察的现象**：无论 dtype 多大，**每 repeat 的字节数恒为 256**，变的只是「元素数」。这正是后续公式里反复出现的 `256 / dtypeBytes` 的来源。

**预期结果**：手算结果应与源码里 `DEFAULT_BLOCK_SIZE / dtypeSize`（fp32 → 64）一致。无需运行，纯阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DEFAULT_REPEAT_STRIDE` 恰好是 8 而不是 1？
**答案**：repeat 步进的单位是 block，而一个 repeat 本身就占 8 个 block（`BLK_NUM_PER_REP = 8`）。步进 8 个 block 正好让下一 repeat 紧接上一 repeat 的末尾，即「连续访问」的默认布局。

**练习 2**：`repeatTimes` 的类型是 `uint8_t`，这意味着它的最大值是多少？由哪个常量约束？
**答案**：`uint8_t` 最大 255，对应 `MAX_REPEAT_TIMES = 255`。

---

### 4.2 mask 长度推导与 Tensor 溢出判断（数学模型）

这是本讲的**数学核心**，也是本讲代码实践任务所在。它回答一个问题：**给定 repeat/mask/stride，一个 Tensor 至少要多大才不会越界？** 推导分三步。

#### 4.2.1 概念说明

- **第一步：mask 决定每个 repeat 算多少元素。** mask 有两种模式：
  - **normal 模式（位掩码）**：mask 是一串 bit，每个 bit 对应一个元素，置 1 表示参与计算。mask 最多 128 bit（high 64 + low 64），用长度为 2 的数组 `[maskHigh, maskLow]` 表示。
  - **counter 模式（计数）**：mask 直接是一个数字，表示「参与计算的元素个数」。
- **第二步：repeat + stride 决定最远访问到哪个 block。** 最后一个 repeat 的起点由 `repeatTimes` 和 `repStride` 决定；该 repeat 内 mask 命中的元素跨多少个 block 由 `maskLen` 和 `blkStride` 决定。
- **第三步：把「最远元素」换算成字节，与 Tensor 容量比较。**

#### 4.2.2 核心流程与公式

**第一步：`GetMaskLength` —— 求「每个 repeat 实际计算多少元素」。**

函数扫描 mask 的**最高置位 bit**，把它换算成元素个数。mask 在内存里是 `[maskHigh, maskLow]`（`maskArray[0]` 是 high，`maskArray[1]` 是 low）。从最高位往低位扫描，找到第一个为 1 的 bit，其位置即为本 repeat 参与计算的元素数：

- 若 `maskHigh == 0`，只扫 low，结果范围 \([0, 64]\)。
- 若 `maskHigh != 0`，扫 high，结果范围 \((64, 128]\)。

对 4 字节及以上的大类型，还要封顶（因为一个 repeat 最多 256 字节）：

\[
\text{maskLen} = \min\bigl(\text{maskLen},\ \frac{\text{DEFAULT\_BLOCK\_SIZE}}{\text{dtypeSize}}\bigr) = \min\bigl(\text{maskLen},\ \frac{256}{\text{dtypeSize}}\bigr)
\]

例如 mask 指向第 100 个元素、但 dtype 是 float32（每 repeat 最多 64 个），则 `maskLen` 被截到 64。

**第二步：`CalculateVectorMaxOffset` —— 求「最远访问元素的下标」（单位：元素）。**

设 `blockLen = ONE_BLK_SIZE / dtypeBytes`（每 block 的元素数，如 fp32 为 8），则：

\[
\text{blkNumLastRep} = \lceil \text{maskLen} / \text{blockLen} \rceil
\]

\[
\text{eleNumLastBlk} = \begin{cases} \text{maskLen} \bmod \text{blockLen}, & \text{若余数} \ne 0 \\ \text{blockLen}, & \text{若整除} \end{cases}
\]

\[
\text{maxOffset} = \bigl((\text{repeatTimes}-1)\cdot \text{repStride} + (\text{blkNumLastRep}-1)\cdot \text{blkStride}\bigr)\cdot \text{blockLen} + \text{eleNumLastBlk}
\]

直觉：前半部分定位「最后一个 repeat 里最后一个 block 的起始元素」，再加 `eleNumLastBlk` 走到该 block 内的末元素。

**第三步：`CalculateNeededTensorSize` —— 换算成字节。**

\[
\text{needBytes} = \text{maxOffset} \times \text{dtypeBytes}
\]

最后 `CheckTensorSizeOverflow(needBytes, tensorSize, ...)` 比较 `needBytes <= tensorSize`，不满足即报错。

counter 模式稍特殊：`CounterSplitMainTail` 把「总元素数」拆成「若干个满 repeat（main）+ 一个尾 repeat（tail）」，分别套用上式再取两者的较大值（因为当 `blkStride` 远大于 `repStride` 时，main 的末端可能比 tail 更远）。

#### 4.2.3 源码精读

`GetMaskLength` 的实现，注意 `MASK_HIGH_IDX = 0`、`MASK_LOW_IDX = 1`、`MASK_MAX_ELE_LEN = 64`、`CONST_MASK_VALUE = 0x8000000000000000`（只保留最高位，配合 `>> i` 逐位扫描）：

[GetMaskLength：扫描 mask 最高置位 bit 得到每 repeat 元素数](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L25-L51) —— 末尾的 `if (dtypeSize >= sizeof(uint32_t))` 分支即上面的封顶逻辑。

`CalculateVectorMaxOffset` 的实现，与上面的公式逐项对应：

[CalculateVectorMaxOffset：由 repeat/stride 几何关系算最远访问偏移](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L228-L240) —— 注意 `repeatTimes == 0` 直接返回 0（不访问），以及 `DivCeil` 计算 `blkNumLastRep`。

把偏移换算成字节的封装函数（匿名命名空间，仅供本文件复用）：

[CalculateNeededTensorSize：maxOffset × dtypeBytes，并特判 int4 打包类型](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L244-L260) —— 它根据 `maskArray.size()` 决定走 `GetMaskLength`（normal，size==2）还是直接用 mask 值（counter，size==1）。

normal 模式的越界检查入口：

[CheckTensorOverflowLowNorm：normal 模式下比较 needBytes 与 bufferSize](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L290-L298) —— 用 `ModeType::NORM_MODE` 调 `CheckTensorSizeOverflow`，仅影响日志后缀。

counter 模式的越界检查入口：

[CheckTensorOverflowLowCounter：拆 main/tail 后取较大末端](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L264-L287)。

两层入口 `CheckTensorOverflowLow` 根据 `ModelFactoryGetMaskMode()` 在 normal/counter 间分发：

[CheckTensorOverflowLow：按 mask 模式分发](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L321-L328)。

#### 4.2.4 代码实践（本讲主实践任务）

**实践目标**：亲手把「一个 repeat 所需最小 Tensor 容量」推导一遍，验证对源码的理解。

**操作步骤**：

1. 阅读 [kernel_vec_binary_check.cpp:69-106 的 CheckAllLowLevel](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L69-L106)，找到它构造 `TensorOverflowParams` 并调用 `CheckTensorOverflowLow(maskArray, params, "dstLocal")` 的地方。
2. 假设一组参数：dtype = float32（`dtypeBytes = 4`）、`repeatTimes = 1`、`mask = FULL_MASK`、`blkStride = 1`、`repStride = 8`。
3. 顺着调用链手工计算：
   - `GetMaskLength`：mask 全 1，最高位在第 128 个元素，但 fp32 封顶到 \(256/4 = 64\)，故 `maskLen = 64`。
   - `blockLen = 32/4 = 8`。
   - `blkNumLastRep = ⌈64/8⌉ = 8`；`64 % 8 == 0`，故 `eleNumLastBlk = 8`。
   - `maxOffset = ((1-1)*8 + (8-1)*1)*8 + 8 = 7*8 + 8 = 64`。
   - `needBytes = 64 * 4 = 256`。

**需要观察的现象**：单个 repeat、满 mask、float32，结论恰好是 **256 字节**——即一个 repeat 的字节数。这说明「最小容量」在连续满载时退化成常识。

**预期结果**：再自己算一组 `repeatTimes = 2`、其余相同的情况：`maxOffset = ((2-1)*8 + 7)*8 + 8 = 15*8+8 = 128`，`needBytes = 128*4 = 512` 字节（两个 repeat）。验证通过即说明你掌握了公式。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetMaskLength` 末尾要对 `dtypeSize >= 4` 的类型封顶？对 half（2 字节）封顶吗？
**答案**：mask 最多 128 bit，但一个 repeat 在 4 字节类型下最多只有 \(256/4 = 64\) 个元素。若 mask 指向第 100 个元素，超出 repeat 实际容量的部分是无效的，故封顶到 64。half 每 repeat 128 个元素，与 mask 上限相等，故 `dtypeSize < 4` 时不进封顶分支。

**练习 2**：counter 模式下，为什么要把 main 块和 tail 块的末端「取较大值」而不是相加？
**答案**：main 与 tail 不是「拼接到一起」的关系，而是「同一个 Tensor 上两种访问路径的末端」。当 `blkStride` 远大于 `repStride` 时，main 块最后一个 repeat 的末端可能比 tail 块更远，所以要用 `std::max` 取两者中更靠后的那个作为越界判据（见源码 `maxOffset = std::max(mainBlkSize, tailRepeatStart + tailBlkSize)`）。

---

### 4.3 Binary 校验器：全流程装配

#### 4.3.1 概念说明

`TikcppVecBinaryCheck` 服务于所有「两个输入 → 一个输出」的向量计算：`Add`、`Sub`、`Mul`、`Compare` 等。它的职责是把 4.2 的数学模型**同时套用到 `dst / src0 / src1` 三个张量**上，并在套用前先做 scope/容量/对齐三道通用检查。

它继承自 `TikcppBaseCheck`，对外暴露两条主路径：

- `CheckAllLowLevel(maskArray)`：低层 API 路径，带 mask，走 4.2 的完整数学模型。
- `CheckAllHighLevel()`：高层 API 路径，不带 mask，改用用户直接给出的 `calCount`（参与计算的总元素数），模型大幅简化。

#### 4.3.2 核心流程

```text
CheckAllLowLevel(maskArray):
  1. UpdateMaskArrayAndCheck(maskArray, maxByteLen)   // 处理"未设 mask"的情况 + mask 合法性
  2. CommonCheck()                                     // scope + 容量 + 对齐
  3. 对 dst / src0 / src1 各调用一次：
        CheckTensorOverflowLow(maskArray, {size, dtype, repeat, blkStride, repStride}, name)
     （Compare 走专用 Cmp 分支，因为其输出是 uint8 比特打包）

CheckAllHighLevel():
  1. CommonCheck()
  2. 对 dst / src0 / src1 各调用一次：
        CheckTensorOverflowHigh(dtypeBytes, size, calCount, name)
```

注意第 1 步 `UpdateMaskArrayAndCheck` 的一个重要细节：如果用户代码没有显式设置 mask（`MaskSetter::GetMask() == false`），校验器会用寄存器里的默认 mask 值替换 `maskArray`，并对 4 字节及以上类型把 `maskHigh` 清零（因为大类型一个 repeat 用不满 128 bit）。

#### 4.3.3 源码精读

类的声明，可见其以 `VecBinaryApiParams&` 为唯一状态，所有检查方法都围绕它展开：

[TikcppVecBinaryCheck 继承 TikcppBaseCheck](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_vec_binary_check.h#L22-L36)。

低层主流程，重点看它如何为 dst/src0/src1 **各构造一份 `TensorOverflowParams`**：

[CheckAllLowLevel：先做 mask/scope/容量/对齐，再分别检查三个张量越界](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L69-L106) —— 注意 `apiName == "Compare"` 时 dst 走 `CheckCmpTensorOverflowLowNorm`，其余走通用 `CheckTensorOverflowLow`。

通用前置检查 `CommonCheck`：要求三个张量都在 UB 上（`HardWareIndex::UB`）、不超过 UB 容量上限、地址 32 字节对齐：

[CommonCheck：scope + bufferSize + 对齐](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L49-L67) —— `supportPos = "VECIN/VECOUT/VECCALC"` 是逻辑位置白名单，最终仍映射到物理位置 UB。

[CheckAddrAlign：三个张量起始地址按 ONE_BLK_SIZE(32B) 对齐](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L122-L128)。

Compare 的专用越界公式（因为比较结果是 1 bit/元素，打包成 uint8）：

[CalculateNeededCmpTensorSize：repeat × 256 / srcDtypeBytes / 8 字节](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L30-L39) —— 除以 8 是因为 8 个比较结果打包成 1 字节。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 binary 指令从入口函数到数学模型的完整调用链。

**操作步骤**：

1. 在 [kernel_check_util.cpp:101-120](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L101-L120) 找到 `CheckFuncVecBinaryImpl` 的三个重载（分别对应「mask 数组」「单个 mask」「不带 mask」三种调用形式）。
2. 观察它们都做同一件事：构造 `TikcppVecBinaryCheck chkIns{intriName, chkParams}`，然后调 `CheckAllLowLevel` 或 `CheckAllHighLevel`。
3. 注意 `mask[2]` 数组到 `maskArray` 的下标翻转：入口传的是 `{mask[0], mask[1]}`（low, high），而校验器内部用的是 `{mask[1], mask[0]}`（high, low），即 [kernel_check_util.cpp:104-105](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L104-L105) 的 `{mask[1], mask[0]}`。

**需要观察的现象**：`ASCENDC_CHECK_INTRI_NAME` 宏在每个入口函数开头都会检查 `intriName` 非空，空则直接 `return false`。

**预期结果**：能画出 `CheckFuncVecBinaryImpl → chkIns.CheckAllLowLevel → CommonCheck + CheckTensorOverflowLow → CalculateNeededTensorSize → GetMaskLength + CalculateVectorMaxOffset` 的完整调用栈。纯阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CommonCheck` 要求 dst/src0/src1 **都必须在 UB** 上？
**答案**：向量计算指令（Vector 部件）的源和目的都必须位于 Unified Buffer（UB）。GM/L1/L0 等其他存储不能直接参与向量运算，需先用 `DataCopy` 搬到 UB。这与 u4-l2 讲过的搬运类 scope 规则一致。

**练习 2**：`CheckAllHighLevel` 与 `CheckAllLowLevel` 在「越界判断」上的本质区别是什么？
**答案**：低层要自己**推导**访问范围（经 `GetMaskLength` + `CalculateVectorMaxOffset`），因为低层 API 的语义是 `repeat/mask/stride`；高层则由用户直接给出 `calCount`（总计算元素数），校验器只需 `needSize = dtypeBytes × calCount`，无需推导几何。两者最终都落到 `CheckTensorSizeOverflow` 比较 expectedSize ≤ tensorSize。

---

### 4.4 Reduce 校验器：work tensor 的特殊处理

#### 4.4.1 概念说明

归约指令（`ReduceSum` / `ReduceMax` / `ReduceMin`）是「多进一出」：每个 repeat 把 8 个 block 的输入**归约成少量输出**（默认每 repeat 产出 `VREDUCE_PER_REP_OUTPUT = 2` 个元素）。这带来与 binary 完全不同的校验重点：

- **dst（输出）很小**：只需容纳 `repeatTimes × 2` 个元素，用 `CheckDstTensorSizeRange` 检查。
- **src0（输入）很大**：仍可复用 binary 的 `CheckTensorOverflowLow/High` 检查。
- **work tensor（中间缓冲）最复杂**：归约需要中间空间暂存部分和，其容量随是否计算索引（`calIndex`）以及归约类型（Sum 与 Max/Min）而变，是 reduce 校验器的核心难点。

#### 4.4.2 核心流程

```text
CheckAllLowLevel(maskArray):           CheckAllHighLevel():
  UpdateMaskArrayAndCheck                CommonCheck()
  CommonCheck()                          CheckTensorOverflowHigh(src0...)
  CheckTensorOverflowLow(src0...)        按 apiName 检查 work tensor：
  按 apiName 检查 work tensor:              ReduceSum  → CheckWorkTensorSizeEqual
      ReduceSum → CheckWorkTensorSizeEqual    ReduceMax  → CheckWorkTensorOffset
      ReduceMax → CheckWorkTensorOffset       其余(Min) → CheckWorkTensorOffset
      其余(Min)→ CheckWorkTensorOffset
```

`CommonCheck` 在 reduce 里多了一条 `CheckAllDtypeBytes`：要求 `dst/src0/work` 三者 dtype 字节数相同。

work tensor 的难点集中在 `CheckWorkTensorOffset`：当 `calIndex == true` 时，它需要**迭代式**地推导中间结果占多少元素——先算 it1（`repeatTimes × 2`），对齐后作为 it2 的输入再归约一次（`ReduceBodyCal`），以此类推，直到收敛。这就是 `ReduceBodyCal` 与 `AlignStartPos` 存在的原因。

#### 4.4.3 源码精读

低层与高层主流程：

[CheckAllLowLevel：mask + 通用检查 + src0 越界 + work tensor 分支](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L262-L284) 与 [CheckAllHighLevel：仅用于 level-2 reduce 接口](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L214-L228)。

work tensor 容量推导核心，注意它按 it1 → it2 → it3 的迭代结构，每一轮用 `ReduceBodyCal` 计算本轮输出并对齐到下一轮起点：

[CheckWorkTensorOffset：迭代推导 work tensor 所需元素数](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L92-L147)。

[ReduceBodyCal：单轮归约的 body/tail 拆分与对齐](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L47-L76) —— `bodyRepTimes = preDataCount / elementNumPerRep` 是完整的 repeat 数，`hasTail` 处理余数。

[AlignStartPos：把元素位置按 32 字节块对齐](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L21-L30) —— `DivCeil(startPos*byteLen, ONE_BLK_SIZE) * ONE_BLK_SIZE / byteLen`。

ReduceSum 的特例——直接要求 work 容量 ≥ `repeatTimes × dtypeBytes`：

[CheckWorkTensorSizeEqual](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L149-L161)。

dst 输出范围——是否算索引决定 `needCount` 是 1 还是 `VREDUCE_CALL_INDEX_COUNT(=2)`：

[CheckDstTensorSizeRange](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L163-L180)。

#### 4.4.4 代码实践

**实践目标**：对比 `ReduceSum` 与 `ReduceMax` 对 work tensor 的要求差异。

**操作步骤**：

1. 在 [CheckAllHighLevel](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_reduce_check.cpp#L214-L228) 中确认：`ReduceSum` 调 `CheckWorkTensorSizeEqual`，而 `ReduceMax`/`ReduceMin` 调 `CheckWorkTensorOffset`。
2. 阅读两者实现：`CheckWorkTensorSizeEqual` 只比较 `repeatTimes × dtypeBytes`（线性、简单）；`CheckWorkTensorOffset` 则需多轮迭代。
3. 思考原因：Sum 的中间结果布局是可线性预测的；而 Max/Min（尤其是带 `calIndex` 时）需要保存「当前最大值及其索引」，布局随迭代轮次收敛，必须逐轮推导。

**需要观察的现象**：`CheckWorkTensorOffset` 在 `calIndex == false` 时会提前 return（退化为简单的 `CheckCheckWorkSize`），只有带索引时才走完整迭代。

**预期结果**：能用一句话说明「Sum 走线性公式、Max/Min 走迭代公式」的原因。纯阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：reduce 的 `CommonCheck` 比 binary 多了哪一项检查？为什么需要它？
**答案**：多了 `CheckAllDtypeBytes`，要求 `dst/src0/work` 三者 dtype 字节数相同。因为归约过程（尤其是带索引的 Max/Min）会在 work tensor 里反复读写中间结果，类型不一致会导致元素数与字节数换算错乱。

**练习 2**：`VREDUCE_PER_REP_OUTPUT = 2` 这个常量在 reduce 校验里起什么作用？
**答案**：它表示「每个 repeat 产出 2 个输出元素」，是 it1 阶段 `it1OutputCount = perRepOutput × repeatTimes` 的乘数，决定了 dst 与 work tensor 容量推导的起点。

---

### 4.5 广播族：broadcast / gather / scatter 的统一模式

#### 4.5.1 概念说明

`BroadCast`、`Gather`、`Scatter` 等指令属于向量计算族，但语义特殊——它们**不逐元素掩码**（广播就是把一个数据铺满，gather/scatter 是按索引搬运），因此 mask 被固定为 `FULL_MASK`，绕过 `GetMaskLength` 的扫描。它们大多调用基类提供的特化路径（如 `CheckTensorOverflowLowBrcb`）。

#### 4.5.2 核心流程与源码精读

广播指令的入口直接构造固定 mask（dtype 为 4 字节时 highMask 置 0，否则全 1）：

[CheckFunBcBImpl：固定 {highMask, lowMask} 后调用广播校验器](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L244-L254)。

广播校验器的 dst 走 `CheckTensorOverflowLowBrcb`（内部把 mask 强制设为 `FULL_MASK`，再走 normal 公式），src 另用 `CheckSrcTensorOverflow`（按 `repeatTimes × BLK_NUM_PER_REP` 估算源端元素数）：

[TikcppVecBroadCastCheck::CheckAllLowLevel](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_broadcast_check.cpp#L34-L61)。

基类的 brcb 特化路径，可见它就是把 mask 写死成全 1，复用 normal 的越界公式：

[CheckTensorOverflowLowBrcb：mask 固定 FULL_MASK 后复用 normal 公式](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L346-L351)。

gather/scatter 的入口同样可在 [kernel_check_util.cpp:313-355](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L313-L355) 找到，模式一致：构造对应校验器 → `CheckAllLowLevel(mask)` / `CheckAllHighLevel()`。

#### 4.5.3 小练习与答案

**练习**：为什么广播指令不需要 `GetMaskLength`，而 binary 需要？
**答案**：广播是把同一个源数据复制到目的的每个位置，目的端「全有效」，不存在「只算某些元素」的语义，所以 mask 固定为 `FULL_MASK`，`maskLen` 直接等于一个 repeat 的满元素数，无需扫描 bit。

---

## 5. 综合实践

把本讲的参数模型与数学模型串起来，完成一次完整的「手工校验」。

**场景**：用户写了一条 `Add` 指令，参数如下（float32）：

- `repeatTimes = 3`，`mask = FULL_MASK`（即 `[maskHigh=0xFFFFFFFFFFFFFFFF, maskLow=0xFFFFFFFFFFFFFFFF]`）。
- `dstRepeatStride = src0RepeatStride = src1RepeatStride = 8`，`blkStride = 1`。
- 三个张量分配的 `size` 都是 `bufSize` 字节。

**任务**：

1. 走 `CheckAllLowLevel` 的流程，列出它会依次执行哪些检查（mask 更新、scope、容量、对齐、三个张量越界）。
2. 手算 `dstLocal` 所需最小字节数（提示：先 `GetMaskLength` → 封顶 64 → `CalculateVectorMaxOffset` → ×4 字节）。
3. 回答：`bufSize` 至少要多少字节这条指令才不报越界错误？

**参考答案**：

1. 流程见 4.3.2 的伪代码：`UpdateMaskArrayAndCheck` → `CommonCheck`（dst/src0/src1 均 UB、不超 UB 容量、32B 对齐）→ 对三个张量各 `CheckTensorOverflowLow(maskArray, {size,4,3,1,8,false}, name)`。
2. `maskLen = min(128, 64) = 64`；`blockLen = 8`；`blkNumLastRep = 8`，`eleNumLastBlk = 8`；`maxOffset = ((3-1)*8 + (8-1)*1)*8 + 8 = (16+7)*8+8 = 184`；`needBytes = 184*4 = 736` 字节。
3. 三个张量需求相同，故 `bufSize ≥ 736` 字节（且需 32 字节对齐）。

> 若本地已按 u1-l4 编译安装了 cpudebug，可写一个故意把 `bufSize` 设成 512 字节的 add 样例，CPU 域运行后应在 `*_npuchk.log` 中看到类似「tensor size needs to be at least 736 bytes」的报错；若暂无环境，此步标注为「待本地验证」。

## 6. 本讲小结

- 向量指令的访存由 **block（32B）/ repeat（256B = 8 block）/ repeatTimes（≤255）/ mask / stride** 共同决定，stride 的单位是 block。
- 越界判断的三步数学模型：`GetMaskLength`（mask → 每 repeat 元素数）→ `CalculateVectorMaxOffset`（repeat+stride → 最远元素下标）→ `× dtypeBytes` → 与 `tensorSize` 比较。
- `TikcppVecBinaryCheck` 把这套模型同时套到 `dst/src0/src1`，前置 `CommonCheck`（scope/容量/对齐），并在 `Compare` 时走比特打包专用公式。
- `TikcppVecReduceCheck` 的难点是 **work tensor**：Sum 走线性公式，Max/Min（带 `calIndex`）走多轮迭代公式 `CheckWorkTensorOffset`。
- 广播族（broadcast/gather/scatter）mask 固定为 `FULL_MASK`，复用基类的 `CheckTensorOverflowLowBrcb` 等特化路径，绕过 `GetMaskLength`。
- 所有失败都经 `ASCENDC_CHECK` 短路返回并记入 `*_npuchk.log`，供 u5 的 `ascendc_npuchk_report.py` 解析。

## 7. 下一步学习建议

本讲讲完了向量计算类校验器，至此 api_check 的「基类（u4-l1）+ 搬运类（u4-l2）+ 向量类（本讲）」三大块已完备。建议：

1. 进入 **u5（NPU Check 工具）**，看这些校验失败如何在 CPU 域执行时被收集、落盘成 `*_npuchk.log`，并用 Python 脚本解析定位到源码行。
2. 若想横向扩展，阅读 [kernel_vec_gather_mask_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_gather_mask_check.cpp) 等其它 `kernel_vec_*_check.cpp`，它们都复用本讲讲过的 `TensorOverflowParams` + `CheckTensorOverflowLow` 模型，只是参数几何略有不同。
3. 在 **u10-l1（扩展 API 校验器）** 里，你会亲手新增一个校验器，那时本讲的「继承基类 + 复用通用 util」流程将直接派上用场。
