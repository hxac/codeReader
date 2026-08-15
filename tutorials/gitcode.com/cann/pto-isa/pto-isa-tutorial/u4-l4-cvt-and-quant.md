# 类型转换与量化指令：TCvt、SetQuant、TQuant/TDequant

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 TCVT 的类型转换约束：支持哪些 (源类型 → 目标类型) 组合、RoundMode（舍入模式）与 SaturationMode（饱和模式）各自控制什么。
2. 理解 PTO 中「量化参数」的两条独立通路：
   - 显式量化指令 TQUANT/TDEQUANT——scale/offset 作为 tile 操作数直接传入；
   - SET_QUANT_SCALAR/SET_QUANT_VECTOR——把量化参数写进硬件配置寄存器，供后续 TPUSH（写出流水）在搬出时「顺手」量化。
3. 能基于 CPU 仿真写一个 fake-quantize 计算：量化到 int8 再反量化，并计算最大绝对误差。

## 2. 前置知识

本讲假设你已完成 u4-l1～u4-l3，熟悉以下概念（不再重复展开）：

- **Tile 与有效区**：片上固定形状 2-D 缓冲，指令语义只在 validRow/validCol 决定的有效区内定义。
- **指令三层结构**：公共 API（`TSYNC` 等待事件 + `MAP_INSTR_IMPL` 转发）→ `*_IMPL`（契约检查）→ 内核层（`__ubuf__` 指针 + intrinsic）。
- **TASSIGN**：Manual 模式下手工把片上偏移绑给 Tile。
- **CPU 仿真与 NPU 双实现**：`*_IMPL` 签名相同，按 `__CPU_SIM` / `__CCE_AICORE__` 宏互斥编译。

再补充三个本讲用到的基础概念：

- **舍入模式（RoundMode）**：浮点转整数（或低精度浮点）时，「0.5 之类的中间值往哪边靠」的策略。常见的有四舍五入到偶数（RINT）、四舍五入远离零（ROUND）、向下取整（FLOOR）等。
- **饱和（Saturation）**：浮点 300.0 转成 int8 时怎么办？饱和模式 ON 会「夹紧」到 127；饱和模式 OFF 则按位截断（取低 8 位），行为对齐 PyTorch 的溢出语义。
- **量化（Quantization）**：深度学习推理中把 fp32 权重/激活压成 int8 以省带宽省算力的技术。最常见的是仿射量化：\( q = \mathrm{round}(x / s) + z \)，其中 \( s \) 是 scale（缩放因子），\( z \) 是 offset（零点/zero-point）。反量化即逆运算 \( x \approx (q - z) \cdot s \)。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp) | `QuantType` / `RoundMode` / `SaturationMode` 等枚举定义 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | TCVT / TQUANT / TDEQUANT / SET_QUANT_* 的公共 API 薄壳 |
| [include/pto/npu/a2a3/TCvt.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp) | TCVT 的 A2/A3 NPU 实现（本讲最长的单个实现文件） |
| [include/pto/npu/a2a3/TQuant.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TQuant.hpp) | TQUANT INT8 路径的 A2/A3 实现（五级转换链） |
| [include/pto/npu/a2a3/TDequant.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TDequant.hpp) | TDEQUANT 的 A2/A3 实现（转换 + 减零点 + 乘 scale） |
| [include/pto/npu/a2a3/SetQuantScalar.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetQuantScalar.hpp) | SET_QUANT_SCALAR：标量量化参数写入硬件配置寄存器 |
| [include/pto/npu/a2a3/SetQuantVector.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetQuantVector.hpp) | SET_QUANT_VECTOR：Scaling tile 地址写入 FPC 寄存器 |
| [include/pto/cpu/TQuant.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TQuant.hpp) | TQUANT 的 CPU 仿真实现（逐元素循环） |
| [include/pto/cpu/TDeQuant.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TDeQuant.hpp) | TDEQUANT 的 CPU 仿真实现 |
| [include/pto/cpu/TCvt.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TCvt.hpp) | TCVT 的 CPU 仿真实现（宿主机 C++ 数学函数模拟舍入） |
| [tests/cpu/st/testcase/tquant/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tquant/main.cpp) | TQUANT 的 CPU ST 用例（本讲实践的模板） |
| [tests/cpu/st/testcase/tdequant/tdequant_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tdequant/tdequant_kernel.cpp) | TDEQUANT 的最小 kernel 示例 |
| [docs/isa/TCVT.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TCVT.md) 等 | ISA 文档：TCVT / TQUANT / TDEQUANT / SET_QUANT_SCALAR / SET_QUANT_VECTOR |

## 4. 核心概念与源码讲解

### 4.1 TCvt：逐元素类型转换

#### 4.1.1 概念说明

TCVT 是「逐元素、带舍入策略的类型转换」：对有效区内每个元素执行

\[ \mathrm{dst}_{i,j} = \mathrm{cast}_{\mathrm{rmode}}(\mathrm{src}_{i,j}) \]

它解决的问题是：Cube 算出来的 fp32 累加结果要写回 fp16 的 GM、int8 量化码要还原成浮点参与后续计算、bf16 输入要转 fp16 才能进某条向量指令……这些「纯精度变换、不改变数值排布」的操作都需要一条统一的转换指令。

TCVT 有两个正交的策略旋钮：

- **RoundMode（舍入模式）**：只在浮点 → 整数 / 低精度浮点时有意义。
- **SaturationMode（饱和模式）**：只在浮点 → 整数（窄化）时有意义，决定溢出是「夹紧」还是「截断」。

#### 4.1.2 核心流程

NPU（A2/A3）实现是一条自顶向下的分派链：

```text
TCVT(dst, src, mode[, satMode])          公共 API（pto_instr.hpp，薄壳）
  └─ TCVT_IMPL                            计算 repeat 配置、按类型对选默认饱和模式
       └─ TCvt<...>                        内核：ApplySatMode 设 CTRL[59] → Head + Tail
            ├─ TCvtHead                    对齐主区：整 repeat 块循环
            │    └─ GenCastCall            编译期按 (DstDType, SrcDType) 分派
            │         └─ GenCastCallXxxToYyy   选 RoundMode → 单条 vconv_* intrinsic
            └─ TCvtTail                    尾区：SetContinuousMask 掩码 + 小 repeat
```

关键点：

1. **repeat 配置**由 `ComputeTCvtRepeatConfig` 按源/目的类型宽度算出——每次 repeat 固定搬 256 字节（`REPEAT_BYTE`），元素个数随类型宽度变化：fp32 每 repeat 64 个、fp16 128 个；int4 因为两个元素打包一个字节单独处理。
2. **有效区裁剪**：列方向按 `dst.GetValidCol()` 切成「整 repeat 主区 + 尾区」，行方向用 `validRow` 限定循环次数——与 TLOAD 的有效区语义一致。
3. **饱和模式是硬件控制位**：CTRL 寄存器第 59 位（`SAT_MODE_BIT`），0 开饱和、1 关饱和；进内核前设置、退出前恢复，避免污染后续指令。

#### 4.1.3 源码精读

**枚举定义**。[type.hpp:249-257](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L249-L257) 定义了 7 种舍入模式（RINT 即「四舍五入到偶数」，是默认推荐）：

```cpp
enum class RoundMode : uint8_t {
    CAST_NONE = 0,
    CAST_RINT = 1,  // round to nearest, tie to even
    CAST_ROUND = 2, // round to nearest, tie away from zero
    CAST_FLOOR = 3, // round to minus infinity
    ...
};
```

[type.hpp:325-331](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L325-L331) 定义饱和模式（注释点明对应硬件 CTRL bit 59 = 0/1）：

```cpp
enum class SaturationMode : uint8_t {
    ON = 0,   // CTRL bit 59 = 0，饱和：夹紧到目标类型范围
    OFF = 1,  // CTRL bit 59 = 1，不饱和：按位截断
};
```

**公共 API**。[pto_instr.hpp:1289-1300](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1289-L1300) 是最常用的两个重载——带或不带显式 `SaturationMode`，都是「TSYNC 等待 → 转发 IMPL → 返回 RecordEvent」的标准薄壳：

```cpp
PTO_INST RecordEvent TCVT(TileDataD& dst, TileDataS& src, RoundMode mode, SaturationMode satMode, ...)
PTO_INST RecordEvent TCVT(TileDataD& dst, TileDataS& src, RoundMode mode, ...)
```

**编译期类型分派**。[TCvt.hpp:789-880](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L789-L880) 的 `GenCastCall` 是一个巨大的 `if constexpr` 链：编译期根据 `(TileDataD::DType, TileDataS::DType)` 把调用路由到具体的 `GenCastCallXxxToYyy`。例如 `half → int8` 走：

```cpp
} else if constexpr (
    std::is_same<typename TileDataD::DType, int8_t>::value &&
    std::is_same<typename TileDataS::DType, half>::value) { // half to int8
    GenCastCallFp16ToInt8<TileDataD, TileDataS>(...);
}
```

**单条 intrinsic 的舍入分派**。以 [TCvt.hpp:87-115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L87-L115) 的 FP32→FP16 为例，每个 RoundMode 对应一条 `vconv_f322f16{r,a,f,c,z,o}` intrinsic（后缀字母即舍入策略）：

```cpp
case RoundMode::CAST_RINT:
    vconv_f322f16r(dst, src, repeatNum, ...);
    break;
case RoundMode::CAST_ROUND:
    vconv_f322f16a(dst, src, repeatNum, ...);
    break;
```

这再次印证 u4-l1 的结论：PTO 指令贴底层 intrinsic，1:1 映射时实现极薄。

**Head/Tail 与饱和开关**。[TCvt.hpp:1108-1131](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L1108-L1131) 是内核主体——先 `ApplySatMode` 设 CTRL[59] 并记住原值，主区走 `TCvtHead`、尾区走 `TCvtTail`，最后 `RestoreSatMode` 恢复：

```cpp
bool originalSatMode = ApplySatMode<NeedSetCtrl>(satMode);
...
if (numRepeatPerLine > 0) {
    TCvtHead<...>(dstPtr, srcPtr, mode, numRepeatPerLine, validRow, ...);
}
dstPtr += numRepeatPerLine * elementsPerRepeat;
...
if (numRemainPerLine > 0) {
    TCvtTail<...>(dstPtr, srcPtr, mode, validRow, numRemainPerLine);
}
RestoreSatMode<NeedSetCtrl>(originalSatMode);
```

**窄化转换的默认饱和策略**。[TCvt.hpp:1199-1210](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L1199-L1210) 用 `constexpr bool kIsNarrowingCvt` 列出六种窄化转换（fp16→i8/u8、fp32→i16、fp16→i16、i64→i32、i32→i16）；不带显式 `SaturationMode` 的重载（[TCvt.hpp:1298-1306](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L1298-L1306)）对这六种默认 `SaturationMode::OFF`（对齐 PyTorch 截断语义），其余默认 `ON`。

**PyTorch 兼容路径（进阶）**。当 `half → int8` 且饱和关闭时，硬件没有单条「非饱和 narrow」指令，实现退化为 6 步组合（[TCvt.hpp:482-548](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L482-L548)）：`fp16 --饱和--> int32 --> int16 --AND 0xFF--> int16 --> fp16 --> int8`，每步之间 `pipe_barrier(PIPE_V)`。这就是 API 里可选 `tmp` 暂存 tile 的来由（256 字节 scratch）。初学阶段记住结论即可：**窄化 + 非饱和 + PyTorch 对齐 = 多步组合，需要 tmp**。

**支持矩阵**：A2/A3 上支持的组合以 [TCvt.hpp:18-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L18-L28) 文件头的速查表为准（FP32→{FP16,FP32,BF16,I16,I32,I64}；FP16→{FP32,I32,I16,I8,U8,S4}；……），A2A3 与 A5 的差异对照表在 [docs/isa/TCVT.md:109-127](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TCVT.md#L109-L127)。

#### 4.1.4 代码实践

**实践目标**：在 CPU 仿真下跑通 TCVT 的 ST 用例，并对照源码理解有效区裁剪。

1. 打开 [tests/cpu/st/testcase/tcvt/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tcvt/tcvt_kernel.cpp)，浏览 kernel 里 `TCVT` 的调用形式。
2. 按 u1-l3 的方式运行 CPU 仿真测试入口（具体过滤参数先用 `python3 tests/run_cpu.py --help` 确认，**待本地验证**）。
3. 观察现象：用例会造出包含边界值（inf、nan、溢出值）的数据并和 golden 比对。
4. 预期结果：tcvt 用例全部 PASS。

**修改实验**：把 kernel 中某处 `RoundMode::CAST_RINT` 改成 `CAST_FLOOR`，观察哪些元素的 golden 发生变化（提示：只有恰好落在 x.5 附近的元素会变）。改完记得还原。

#### 4.1.5 小练习与答案

**练习 1**：`TCVT(int8Tile, fp16Tile, RoundMode::CAST_RINT)` 输入 300.0，输出是多少？带显式 `SaturationMode::ON` 和 `OFF` 分别呢？

答案：`ON` 时饱和夹紧到 127；`OFF` 时按 PyTorch 语义截断低 8 位——300 的二进制低 8 位是 `00101100`（44），输出 44。这正是 [TCvt.hpp:73-75](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L73-L75) 注释里给出的例子。

**练习 2**：为什么不带 `SaturationMode` 的 `TCVT` 重载对窄化转换默认 `OFF`、对其他转换默认 `ON`？

答案：见 [TCvt.hpp:1294-1297](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L1294-L1297) 的注释——为了让 PTO kernel 的数值行为与 PyTorch 的 cast 语义（溢出截断）对齐，方便用 PyTorch/numpy 做 golden 比对；而非窄化路径不存在「溢出截断」问题，用硬件原生饱和更安全。

**练习 3**：`elementsPerRepeat` 对 fp32→fp16 是多少？

答案：64。repeat 固定 256 字节，按源/目的中较宽的类型（fp32，4 字节）计算：256/4 = 64。见 [TCvt.hpp:1184-1196](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TCvt.hpp#L1184-L1196) 的 `ComputeTCvtRepeatConfig`。

### 4.2 SetQuant 系列：量化参数配置指令

#### 4.2.1 概念说明

首先要建立一个重要的认知：**PTO 里有两条互不相同的「量化参数」通路**，初学者极易混淆：

| 通路 | 指令 | 参数去向 | 消费者 |
|---|---|---|---|
| 显式量化 | `TQUANT` / `TDEQUANT` | scale/offset 作为 **tile 操作数** 直接传给指令 | 指令本体 |
| 写出时量化 | `SET_QUANT_SCALAR` / `SET_QUANT_VECTOR` | 写入**硬件配置寄存器** | 后续的 `TPUSH`（fixpipe 写出流水） |

SET_QUANT 系列解决的问题是「边搬出边量化」：Cube 累加器里的 fp32 结果通过 TPUSH 写回 GM 时，硬件 fixpipe 可以顺带做一次乘 scale 的量化，免去单独一条向量指令。为此必须先把量化参数配置到硬件寄存器里——这就是 SET_QUANT_SCALAR（标量 scale，整个 tile 共用一个值）和 SET_QUANT_VECTOR（向量 scale，用一个 Scaling tile 提供参数）的职责。**它们与 TQUANT 没有调用关系**——TQUANT 的 scale 是直接传 tile 的。

#### 4.2.2 核心流程

```text
SET_QUANT_SCALAR<OutT>(preQuantScalar)
  └─ 把 float 的 32 位比特模式放进 64 位配置值
     └─ OutT 是 8 位类型时，在第 46 位编入符号标志（int8=1，uint8=0）
        └─ set_quant_pre(配置值)        ← 写硬件量化配置寄存器

SET_QUANT_VECTOR(fpTile)                 fpTile 必须是 TileType::Scaling
  └─ 取 fpTile 的 __fbuf__ 地址
     └─ 地址 >> 7 << 8（换算成硬件 QUANT_PRE 地址格式，128 字节为单位）
        └─ set_fpc(地址)                ← 写硬件 FPC 寄存器

随后的 TPUSH 读取该寄存器，在写出时执行量化
```

#### 4.2.3 源码精读

**SET_QUANT_SCALAR 的 NPU 实现**只有 10 行——[SetQuantScalar.hpp:16-25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetQuantScalar.hpp#L16-L25)：

```cpp
template <typename OutT>
PTO_INTERNAL void SET_QUANT_SCALAR_IMPL(float preQuantScalar)
{
    uint64_t quantValue = static_cast<uint64_t>(*reinterpret_cast<int32_t*>(&preQuantScalar));
    if constexpr (sizeof(OutT) == 1) {
        constexpr bool sign = (std::is_same_v<OutT, int8_t>) ? true : false;
        quantValue = (quantValue & ~(static_cast<uint64_t>(1) << 46)) | (static_cast<uint64_t>(sign) << 46);
    }
    set_quant_pre(quantValue);
}
```

这段代码做了三件事：把 float 按位重解释成 int32 再零扩展到 64 位（保留精确比特模式，不做数值转换）；8 位输出类型在第 46 位编入符号标志；最后 `set_quant_pre` 写入硬件量化配置寄存器。

**SET_QUANT_VECTOR 的 NPU 实现**——[SetQuantVector.hpp:16-31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetQuantVector.hpp#L16-L31)：

```cpp
template <typename FpTileData>
__tf__ PTO_INTERNAL void SET_QUANT_VECTOR(typename FpTileData::TileDType __in__ fp)
{
    __fbuf__ typename FpTileData::DType* fpAddr = (__fbuf__ typename FpTileData::DType*)__cce_get_tile_ptr(fp);
    // 7 is for QUANT_PRE_ADDR in unit of 128(2^7) bytes, 8 is for FPC[15:8] Quant_PRE parameter address
    uint64_t fpTileAddr = ((uint64_t)fpAddr >> static_cast<uint64_t>(7)) << 8;
    set_fpc(fpTileAddr);
}
```

两个细节：`static_assert` 强制输入必须是 `TileType::Scaling` 类型的 tile（编译期拦截误用）；地址换算 `>>7 <<8` 把 tile 地址翻译成硬件 FPC 寄存器 `[15:8]` 域期待的「128 字节粒度参数地址」格式——这是典型的「ISA 层给硬件寄存器格式打工」的代码。

**真实用法**。CPU 仿真版的 SET_QUANT_SCALAR（[cpu/SetQuantScalar.hpp:16-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/SetQuantScalar.hpp#L16-L28)）把配置值写进 `NPUMemoryModel` 模拟的寄存器区。完整调用场景见 tpushpop_fixpipe 用例（[tpushpop_fixpipe_kernel.cpp:200-201](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop_fixpipe/tpushpop_fixpipe_kernel.cpp#L200-L201)）：

```cpp
SET_QUANT_SCALAR<OutT>(2.0f);
TPUSH<MatPipe, AccTile, FixpipeConfig>(pipe, accTile);
```

以及向量版（[tpushpop_fixpipe_kernel.cpp:319-321](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop_fixpipe/tpushpop_fixpipe_kernel.cpp#L319-L321)）：先把参数 TMOV 到 Scaling tile，再 `SET_QUANT_VECTOR(fbTile)`，随后 TPUSH。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读确认「SET_QUANT → TPUSH」的配对关系（源码阅读型实践，无需硬件）。

1. 通读 [tests/cpu/st/testcase/tpushpop_fixpipe/tpushpop_fixpipe_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop_fixpipe/tpushpop_fixpipe_kernel.cpp)。
2. 找出两处 SET_QUANT 调用，记录：标量版传的值、向量版的 Scaling tile 从哪来（哪条 TMOV 的目的地）。
3. 观察现象：SET_QUANT 与 TPUSH 之间没有插入其他改变量化参数的指令。
4. 预期结果：你能画出「量化参数 → 配置寄存器 → TPUSH 消费」的一行数据流图。

#### 4.2.5 小练习与答案

**练习 1**：`SET_QUANT_SCALAR<int8_t>(0.5f)` 和 `SET_QUANT_SCALAR<uint8_t>(0.5f)` 写入的 64 位配置值有什么区别？

答案：两者低 32 位都是 0.5f 的比特模式 `0x3F000000`，但第 46 位不同：int8 版置 1（有符号），uint8 版置 0。硬件据此决定写出时按有符号还是无符号解释量化结果。

**练习 2**：能不能用 `SET_QUANT_SCALAR` 给 `TQUANT` 提供 scale？

答案：不能。TQUANT 的 scale 是 tile 操作数（见 4.3 节），SET_QUANT_* 配置的是 TPUSH 写出通路读的硬件寄存器，两条通路互不相干。把两者混用是本讲最常见的错误。

**练习 3**：SET_QUANT_VECTOR 为什么强制 `TileType::Scaling`？

答案：Scaling 是 PTO 专门为缩放因子数据预留的 tile 位置类型（u2-l2 提过 TileType 参与指令的编译期检查）。硬件 FPC 寄存器期待的是 `__fbuf__`（fixpipe 缓冲）上的参数地址，用 TileType 把「这块缓冲的角色」在类型系统里钉死，防止把普通 UB 数据误当量化参数。[SetQuantVector.hpp:29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/SetQuantVector.hpp#L29) 的 static_assert 即此检查。

### 4.3 TQuant/TDequant：显式量化与反量化指令

#### 4.3.1 概念说明

TQUANT 把高精度 tile（A2/A3 上仅支持 **FP32 输入**）压成低精度格式；TDEQUANT 是它的逆运算。两者构成一对「显式、可组合」的量化指令：scale/offset 都是普通 tile 操作数，可以由前序指令（如 per-channel max 的倒数）动态算出。

TQUANT 支持两大目的地家族（[docs/isa/TQUANT.md:11-15](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TQUANT.md#L11-L15)）：

- **INT8（对称/非对称，A2/A3 可用）**：\( q_i = \mathrm{round}(x_i / \mathrm{scale}) + \mathrm{offset} \)，\( q_i \in [-128,127] \)。
- **MX 微缩放格式（MXFP8/MXFP4，A5 专属）**：32 元素一组共享指数，本讲只做导览，细节留到 u5-l5。

TDEQUANT 是仿射反量化：\( \mathrm{dst}_{i,j} = (\mathrm{src}_{i,j} - \mathrm{offset}_i) \cdot \mathrm{scale}_i \)，其中 scale/offset 是 **per-row（按行）** 的 FP32 参数，沿列方向广播。

#### 4.3.2 核心流程

**TQUANT INT8（A2/A3）**——为了规避硬件直接 fp32→int8 的双重舍入误差，实现走一条「五级转换链」：

```text
src(fp32)
  ├─ TROWEXPANDMUL(src, src, scale)      每行乘以该行 scale（参数按行广播）
  ├─ [ASYM] TROWEXPANDADD(src, src, offset)   非对称再加零点
  ├─ TCVT: fp32 → s32   （写入 tmp，因为 A3 不支持就地 tcvt）
  ├─ TCVT: s32 → fp16   （写回 src 原地址）
  └─ TCVT: fp16 → int8  （饱和 ON，写入 dst）
```

数学上等价于 \( q = \mathrm{rint}(\mathrm{rint}(\mathrm{rint}(x \cdot s)) ) \to \mathrm{sat}_{[-128,127]} \)，中间借 fp16 是硬件转换通路的约束。**注意**：文档公式写的是 \( x / \mathrm{scale} \)，而实现（CPU 与 NPU 一致）把 scale tile 的值直接作为**乘法因子**使用——即 tile 里存的是公式中 \( 1/\mathrm{scale} \)。ST 用例里 scale 取 4.0、3.0 等大于 1 的值，按乘法因子理解才与 golden 一致。

**TDEQUANT（A2/A3）**——两步走，全程向量流水、无需 tmp：

```text
src(int8/int16)
  ├─ ConvertForDequant: int8/int16 → fp32（vconv，逐行）
  └─ ApplyScaleAndOffset: 逐行取 offset_i、scale_i（Scalar 流水读标量）
       vadds(dst, dst, -offset)  →  vmuls(dst, dst, scale)
```

#### 4.3.3 源码精读

**QuantType 枚举**。[type.hpp:198-203](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L198-L203)——INT8_SYM（对称，输出 int8）与 INT8_ASYM（非对称，输出 uint8）是 A2/A3 可用的两条路径：

```cpp
enum class QuantType {
    MXFP8 = 0,
    MXFP4_E2M1 = 1,
    INT8_SYM = 2,
    INT8_ASYM = 3,
};
```

**公共 API**。[pto_instr.hpp:2502-2518](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L2502-L2518) 是 INT8 家族的 TQUANT 重载——scale 是 tile 引用，offset 可空指针（SYM 时不传）：

```cpp
TQUANT(TileDataOut& dst, TileDataSrc& src, TileDataPara& scale, TileDataPara* offset = nullptr, ...)
// 带 tmp 的 A2/A3 版本
TQUANT(TileDataOut& dst, TileDataSrc& src, TileDataPara& scale, TileDataTmp& tmp, TileDataPara* offset = nullptr, ...)
```

**NPU 五级链**。[TQuant.hpp:22-65](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TQuant.hpp#L22-L65)。开头的 static_assert 钉死契约：输入必须 `float32_t`，SYM 输出必须 `int8_t`、ASYM 必须是 `uint8_t`：

```cpp
static_assert(std::is_same<T, float32_t>::value, "Fix: Input has to be float 32");
if constexpr (quant_type == QuantType::INT8_SYM) {
    static_assert(std::is_same<U, int8_t>::value, "Fix: Quant INT8 sym: Out data type has to be int8");
```

中段先做「乘 scale（加 offset）」，其中 tmp 被复用为 s32 中间缓冲（注释点明 A3 不支持就地 tcvt）：

```cpp
// tmp is reused afterward for fp32->s32 conversion (A3 does not support in-place tcvt).
TROWEXPANDMUL_IMPL(src, src, scale, tmp);
```

随后手工构造两个中间视图 tile（`src_f16`、`src_s32`）并 TASSIGN 到 src/tmp 的地址——这是 Manual 模式下「同一块物理缓冲换类型视角」的标准手法，与 u3-l3 的 TRESHAPE 零拷贝别名一脉相承（Auto 模式下则直接用 `TRESHAPE_IMPL`，见 [TQuant.hpp:51-57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TQuant.hpp#L51-L57) 的 `#ifndef __PTO_AUTO__` 分支）：

```cpp
TCVT_IMPL(src_s32, src, RoundMode::CAST_RINT);        // fp32->s32, dst=tmp src=src
TCVT_IMPL(src_f16, src_s32, RoundMode::CAST_RINT);    // s32->fp16, dst=src src=tmp
TCVT_IMPL(dst, src_f16, RoundMode::CAST_RINT, SaturationMode::ON); // fp16->int8
```

**CPU 仿真实现**。[cpu/TQuant.hpp:494-520](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TQuant.hpp#L494-L520) 是双精度循环——语义一目了然，参数按行读取（`GetParamValue(scale, row, 0)`）：

```cpp
const float invScale = static_cast<float>(cpu_quant::GetParamValue(scale, row, 0));
dst.data()[...] = cpu_quant::ClampInt8(srcValue * invScale);   // SYM
dst.data()[...] = cpu_quant::ClampUint8(srcValue * invScale + zeroPoint);  // ASYM
```

其中 `ClampInt8`（[cpu/TQuant.hpp:86-90](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TQuant.hpp#L86-L90)）= `nearbyint` + clamp 到 [-128, 127]。

**TDEQUANT 的 NPU 实现**。契约检查 [TDequant.hpp:109-133](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TDequant.hpp#L109-L133)：dst/scale/offset 必须 FP32，src 只能 int8/int16，全部行主序，且 src 与 dst 有效区一致。核心 [TDequant.hpp:93-107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TDequant.hpp#L93-L107) 分两步：先 `ConvertForDequant` 把整数码升到 FP32，再 `ApplyScaleAndOffset`。后者（[TDequant.hpp:67-88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TDequant.hpp#L67-L88)）展示了 Scalar 流水与 Vector 流水的握手——用 `set_flag/wait_flag` 事件保护标量读取，然后两条向量指令收尾：

```cpp
T offsetValue = *(offsetPtr + i * scaleRowStride);   // Scalar 流水读标量
set_flag(PIPE_S, PIPE_V, EVENT_ID0); wait_flag(...);
vadds(dstNext, dstNext, -offsetValue, 1, 1, 1, 8, 8);   // 减零点
vmuls(dstNext, dstNext, scaleValue, 1, 1, 1, 8, 8);     // 乘 scale
```

**CPU 仿真实现**。[cpu/TDeQuant.hpp:18-37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TDeQuant.hpp#L18-L37) 一行公式直译，注意 per-row 参数的列钳制（`paraCol = min(c, paraCols-1)`，参数 tile 通常每行 1 列）：

```cpp
dst.data()[...] = static_cast<DstDType>(
    (static_cast<DstDType>(srcVal) - static_cast<DstDType>(offsetVal)) *
    static_cast<DstDType>(scaleVal));
```

**最小 kernel 范例**。[tdequant_kernel.cpp:16-49](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tdequant/tdequant_kernel.cpp#L16-L49) 是 TDEQUANT 的完整调用骨架：参数 tile 用 `BLayout::ColMajor` 的 `TileType::Vec` 每行一列，TLOAD 三个输入后一条 `TDEQUANT(dstTile, srcTile, scaleTile, offsetTile)` 搞定，最后 TSTORE 写回。

#### 4.3.4 代码实践：fake-quantize 往返实验

**实践目标**：把一个 FP32 tile 量化到 int8 再反量化（fake-quantize），计算最大绝对误差，理解量化损失的数量级。

**关于任务原型的说明**：任务原型写的是「fp16 tile 用 SetQuantScalar + TQuant」。查源码后有两个修正：(1) A2/A3 的 TQUANT 有 `static_assert(std::is_same<T, float32_t>::value)`，fp16 输入编译不过，需先 TCVT 成 fp32；(2) SET_QUANT_SCALAR 服务的是 TPUSH 写出通路，与 TQUANT 无关（见 4.2 节）。因此实践按「FP32 + TQUANT + TDEQUANT」执行，这才是与真实 API 匹配的做法。

**操作步骤**（CPU 仿真，参照 [tests/cpu/st/testcase/tquant/main.cpp:161-193](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tquant/main.cpp#L161-L193) 的 Int8Sym 用例改写）：

1. 在本地仓库新建一个gtest 文件（或直接改 tquant 用例临时实验），核心代码如下（**示例代码**，仿照上述用例编写，非仓库原有）：

```cpp
#include <gtest/gtest.h>
#include <pto/pto-inst.hpp>
using namespace pto;

TEST(FakeQuant, Int8SymRoundTrip) {
    using SrcTile  = Tile<TileType::Vec, float, 4, 32>;   // 输入 fp32
    using QTile    = Tile<TileType::Vec, int8_t, 4, 32>;  // 量化码
    using DqTile   = Tile<TileType::Vec, float, 4, 32>;   // 反量化结果
    using ParaTile = Tile<TileType::Vec, float, 8, 1, BLayout::ColMajor, 4, 1>;

    SrcTile src; QTile q; DqTile dq; ParaTile scale, dqScale, dqOffset;
    // 1) TASSIGN 依次摆放各 tile（地址不重叠，参照 tquant 用例的累加 addr 写法）
    // 2) 填数据：src 取 [-1, 1] 均布；量化 scale 每行 = 127.0f（乘法因子）
    //    反量化 dqScale 每行 = 1/127.0f，dqOffset 每行 = 0
    TQUANT<QuantType::INT8_SYM>(q, src, scale);
    TDEQUANT(dq, q, dqScale, dqOffset);
    // 3) 逐元素求 |dq - src| 的最大值并 EXPECT 输出
}
```

2. 编译运行方式与 tquant 用例一致（CPU 仿真构建入口见 u1-l3；gtest 目标的挂接可参照 [tests/cpu/st/testcase/tquant/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tquant/CMakeLists.txt)）。具体构建命令**待本地验证**。
3. 观察 `max |dq - src|`，再换更小的 scale（如 63.5）重复一次。

**需要观察的现象**：

- 反量化结果呈「台阶状」——同一量化码映射回同一个浮点值；
- max 误差约等于量化步长的一半，即 \( 1/(2 \times 127) \approx 0.004 \)。

**预期结果**：`TQUANT→TDEQUANT` 往返后 max abs error 在 0.004 左右（精确值**待本地验证**）；scale 减半则误差约翻倍。

#### 4.3.5 小练习与答案

**练习 1**：A2/A3 的 TQUANT 为什么要走 FP32→S32→FP16→INT8 的五级链，而不是一步 FP32→INT8？

答案：见 [docs/isa/TQUANT.md:37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TQUANT.md#L37)——避免双重舍入（double-rounding）：直接 fp32→int8 会先舍到 fp16 精度再舍到整数，两次舍入叠加产生偏差；五级链让中间精度可控，数值结果与参考实现对齐。A5 硬件支持原生广播+转换，无需 tmp。

**练习 2**：TDEQUANT 的 scale/offset 为什么设计成 per-row 而不是 per-tile 标量？

答案：per-row 参数能表达 per-channel 量化——例如按输出通道（矩阵的一行）各配一组 scale/offset，这是量化推理的常态。实现上通过 `vlds BRC_B32` 按 32 字节块沿列广播（[docs/isa/TDEQUANT.md:69-71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TDEQUANT.md#L69-L71)），一行一条 vadds+vmuls 即可完成。若只要 per-tile 标量，把参数 tile 每行填同一个值即可，表达力是超集。

**练习 3**：TQUANT INT8_SYM 输入 0.9、scale（乘法因子）= 127，输出多少？再用 TDEQUANT（dqScale=1/127, offset=0）还原得到什么？

答案：`rint(0.9 × 127) = rint(114.3) = 114`，在 [-128,127] 内，输出 114；反量化 `114 × (1/127) ≈ 0.898`，与 0.9 的误差约 0.002，小于半个量化步长（1/254 ≈ 0.0039）。

## 5. 综合实践

**任务：给 Add 算子加一个「量化写出」开关（源码阅读 + 方案设计）**。

结合 u1-l4 的 Add 示例与本讲知识，设计一个 fp32 Add 的量化推理变体：

1. **读**：回顾 [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) 的 TLOAD→TADD→TSTORE 骨架。
2. **设计 A（显式量化）**：在 TADD 之后插入 `TQUANT<QuantType::INT8_SYM>(qTile, accTile, scaleTile)`，host 侧 golden 同步改为 int8 量化比对。写出需要新增的 tile 声明（注意 dst 是 `int8_t` tile、scale 是 ColMajor 每行一列）和 TASSIGN 地址排布。
3. **设计 B（写出量化）**：若 Add 结果走 TPUSH/fixpipe 写出，则改用 `SET_QUANT_SCALAR<int8_t>(s)` 预设参数。对比 A/B 两条路径各自适合的场景（提示：A 灵活、可继续片上计算；B 省一条向量指令、但只能配固定标量）。
4. **验**：在 CPU 仿真下实现方案 A 并跑通（可复用 4.3.4 的用例工程），测量量化前后的最大误差。

完成本任务后，你就把「计算指令（u4-l1）→ 转换指令（TCVT）→ 量化指令（TQUANT/TDEQUANT）→ 写出配置（SET_QUANT）」串成了一条完整的低精度推理数据通路。

## 6. 本讲小结

- **TCVT** 是逐元素类型转换，两个正交旋钮：RoundMode（7 种舍入策略，映射到 `vconv_*{r,a,f,c,z,o}` intrinsic 后缀）与 SaturationMode（CTRL[59]，决定溢出夹紧还是截断）；实现按 (源, 目标) 类型对在编译期 `if constexpr` 分派，支持矩阵以 TCvt.hpp 文件头速查表为准。
- **窄化转换**（fp16→i8 等 6 种）默认非饱和以对齐 PyTorch；`half→int8` 非饱和路径无单条 intrinsic，退化为 6 步组合并需要 256 字节 tmp tile。
- **量化参数有两条独立通路**：TQUANT/TDEQUANT 的 scale/offset 是 tile 操作数；SET_QUANT_SCALAR/SET_QUANT_VECTOR 写硬件配置寄存器、由后续 TPUSH 消费，二者不能混用。
- **TQUANT INT8（A2/A3）** 输入必须 FP32，实现走「乘 scale →（加 offset）→ FP32→S32→FP16→INT8」五级链以避免双重舍入，tmp 复用为 S32 中间缓冲；实现中 scale tile 的值按乘法因子（即文档公式的 1/scale）使用。
- **TDEQUANT** 是 per-row 仿射反量化 `(src - offset_i) · scale_i`，src 限 int8/int16，dst/scale/offset 必须 FP32，全程向量流水无需 tmp。
- **CPU 仿真**把以上全部化简为逐元素循环（`nearbyint` + `clamp`），只保功能正确；五级链、寄存器编码等通路细节以 NPU 实现为准。

## 7. 下一步学习建议

下一讲（u4-l5）转向**排序与 TopK**：TMrgSort 归并排序指令与 kernels/manual/a2a3/topk 完整算子，其中会再次用到本讲 u4-l3 的 TGather_cmp（逐行阈值比较收集）作为底层积木。

更远的衔接：

- **u5-l5（MX 混合精度）**：本讲只导览了 TQUANT 的 MX 家族（MXFP8/MXFP4、32 元素共享指数、OCP/NV 两种 scale 算法），E8M0 指数布局与 `TMATMUL_MX` 的配合将在那里结合 A5 性能算子展开（CPU 侧的精确编码参考 [include/pto/cpu/TQuant.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TQuant.hpp) 的 `cpu_quant` 命名空间）。
- **SET_QUANT 的消费端**：TPUSH/TPipe 协议在 u3-l2 已讲过绑定，若想看完整的「Cube 累加器 → fixpipe 量化写出」链路，精读 [tests/cpu/st/testcase/tpushpop_fixpipe/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tpushpop_fixpipe/tpushpop_fixpipe_kernel.cpp) 用例。
- 建议顺手阅读 [docs/isa/TQUANT_HIF4.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TQUANT_HIF4.md)，了解 HiFloat4 这类新格式如何挂在同一套 TQUANT 接口下。
