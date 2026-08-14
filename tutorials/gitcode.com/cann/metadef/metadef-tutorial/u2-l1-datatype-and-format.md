# DataType 与 Format：基础枚举体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `DataType` 和 `Format` 两个枚举的定义位置、取值来源（C 枚举）以及为什么这样设计。
2. 熟练使用 `ge` 命名空间下的类型工具函数：`GetSizeByDataType`、`GetSizeInBytes`、`GetPrimaryFormat`、`GetSubFormat`、`GetC0Format`、`GetFormatName` 等。
3. 理解 `Format` 并不只是一个简单枚举——它是一个 32 位整数位域，由 primary format、sub-format、c0_format 三段拼成，并能手工完成「编码 → 解码」。
4. 沿着「`inc/external` 声明 → `base` 实现」的路径（上一讲建立的目录心智模型）读懂 `GetSizeInBytes` 的完整实现链路。

## 2. 前置知识

- **枚举（enum）**：C/C++ 中给一组整数值起名字的方式。本讲的两个核心枚举 `DataType`、`Format` 的每一个取值本质上都是一个整数。
- **位域（bit field）编码**：把多个小整数 packing 进一个 32 位整数的不同比特段。本讲会看到 `Format` 的 32 位被切成 4 段：最低 1 字节存主格式，中间 2 字节存子格式，再往上 4 比特存 C0 格式。
- **ABI 兼容**：metadef 被大量已编译好的组件（ge、各算子仓）依赖，因此对外头文件里的枚举值一旦发布就不能变。这就是为什么 `DataType`/`Format` 的取值不从 0 重新编，而是「转发」自一个稳定的 C 枚举。
- **声明与实现分离**：回顾 u1-l3，`inc/external/graph/types.h` 中带函数体的是 inline 函数（编译期就地展开），只有声明没有体（如 `GetSizeInBytes`）的才需要在 `base/` 下找实现。
- **昇腾的 C0 概念**：昇腾 AI Core 的矩阵计算单元按「块（cube）」处理数据，一个块在 C 通道方向固定取 16 个（fp16）或 32 个（int8）元素，这个块内通道数就是 C0。`NC1HWC0`、`FRACTAL_Z`、`FRACTAL_NZ` 这些格式名里的 C0/Z 就来源于此。本讲不需要深挖硬件，只需知道 C0 是「数据排布的基本颗粒大小」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/graph/c_types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/c_types.h) | 纯 C 枚举 `C_DataType`、`C_Format`，是对外 ABI 的「真值来源」，C++ 枚举只是转发它 |
| [inc/external/graph/types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h) | 本讲主战场：`ge::DataType`、`ge::Format` 枚举，以及一组 inline 位域工具函数与 `GetSizeByDataType` |
| [base/type/types_impl.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc) | `types.h` 中「声明无体」函数的实现侧：`TypeImpl::GetFormatName`、`TypeImpl::GetSizeInBytes` 等 |
| [pkg_inc/graph/def_types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/def_types.h) | 随 CANN 包发布的指针/数值互转小工具（`PtrToValue`、`ValueToPtr` 等），代表 pkg_inc 层的基础类型辅助 |
| [pkg_inc/graph/graph_type_utils.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/graph_type_utils.h) | pkg_inc 层的 `TypeId` 体系（模板特化取类型 ID），为下一讲 AnyValue/TypeId 埋伏笔 |
| [tests/ut/base/testcase/types_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc) | 本讲函数的官方单测，代码实践的参照物 |

## 4. 核心概念与源码讲解

### 4.1 DataType 枚举与 GetSizeByDataType

#### 4.1.1 概念说明

`DataType` 回答的问题是：「这个张量里每个元素是什么类型、占多少空间」。它是整个 CANN 栈最基础的词汇——建图时 `TensorDesc` 要指定它、算子注册时 `OpDef` 要声明支持哪些它、tiling 计算字节数时要以它为依据。

metadef 里 `DataType` 有一个容易忽略的设计：C++ 枚举的每个值都不是手写的整数，而是转发自纯 C 枚举 `C_DataType`。这样做是为了让同一套取值能同时被 C 接口（CANN 对外提供 C API）和 C++ 接口使用，且 C 和 C++ 两侧的值永远一致——这是 ABI 兼容的第一道防线。

#### 4.1.2 核心流程

`GetSizeByDataType(DataType)` 的查表流程：

```text
输入 data_type
  ├── 越界（< 0 或 >= DT_MAX） → 返回 -1（表示"无固定大小/非法"）
  └── 合法 → 返回静态查找表 data_type_size[data_type]
```

两个特殊约定要记住：

1. **返回 -1 的类型**：`DT_STRING`、`DT_STRING_REF`、`DT_UNDEFINED` 这类「长度不定」或「未设置」的类型，表项是 -1。
2. **比特单位偏移**：小于 1 字节的类型（`DT_INT4`、`DT_UINT1`、`DT_FLOAT4_E2M1` 等）的大小存的是 `kDataTypeSizeBitOffset + 比特数`，即 `1000 + n`。调用方拿到大于 1000 的值就知道单位是比特而不是字节。这是「用一个 int 同时表达单位与数值」的经典紧凑技巧。

#### 4.1.3 源码精读

先看 C 枚举的真值来源（节选）：

- [inc/external/graph/c_types.h:17-60](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/c_types.h#L17-L60)：`C_DT_FLOAT = 0` 从 0 开始顺序编号，`C_DT_MAX` 作为边界标记收尾。C++ 侧一切取值以这里为准。
- [inc/external/graph/c_types.h:63-126](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/c_types.h#L63-L126)：`C_Format` 枚举，`C_FORMAT_MAX = 0xff`（这点在 4.2 节会用到——主格式只占 1 字节，`0xff` 恰好是它的容量上限）。

C++ 枚举逐项转发 C 枚举：

- [inc/external/graph/types.h:81-125](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L81-L125)：`enum DataType { DT_FLOAT = ::C_DT_FLOAT, ... }`。注意 `::` 前缀显式指向全局命名空间的 C 枚举。新增类型只能追加在 `DT_MAX` 之前，且顺序必须与 `C_DataType` 完全对齐——中间插一项就会让所有后续值错位，直接破坏 ABI。

查表函数本体：

- [inc/external/graph/types.h:133-184](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L133-L184)：`GetSizeByDataType` 是 inline 函数，内部用 `static int data_type_size[DT_MAX]` 查表。逐项注释标明了每个下标对应的类型；注意第 5 项（下标 5）是 `-1, // reserved`——因为 `C_DataType` 里这个位置没有对应类型，C++ 侧也无枚举名指向它。比特类型示例：`DT_INT4` 的表项是 `kDataTypeSizeBitOffset + 4`（即 1004，含义 4 bit）。越界判断在 [types.h:180-183](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L180-L183)。

配套常量：

- [inc/external/graph/types.h:34-37](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L34-L37)：`kDataTypeSizeBitOffset = 1000`、`kBitNumOfOneByte = 8`、`kBitThreeBytes = 24`，后两个是 4.2 节位域运算的移位数。

官方单测断言（用来校验你的理解）：

- [tests/ut/base/testcase/types_unittest.cc:108-117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L108-L117)：`GetSizeByDataType(DT_FLOAT) == 4`、`(DT_FLOAT16) == 2`、`(DT_INT64) == 8` 等。

#### 4.1.4 代码实践

**实践目标**：用查表函数亲手验证「数值」与「单位偏移」两个约定。

1. 在任意目录新建 `dt_size_demo.cc`（示例代码，非项目原有文件）：

```cpp
// 示例代码：只依赖头文件中的 inline 函数，无需链接 libmetadef
#include <cstdio>
#include "graph/types.h"

int main() {
  const ge::DataType cases[] = {ge::DT_FLOAT, ge::DT_FLOAT16, ge::DT_BF16,
                                ge::DT_INT64,  ge::DT_INT4,    ge::DT_UNDEFINED};
  for (const auto dt : cases) {
    const int size = ge::GetSizeByDataType(dt);
    if (size > ge::kDataTypeSizeBitOffset) {
      printf("bit-type size = %d bits\n", size - ge::kDataTypeSizeBitOffset);
    } else {
      printf("size = %d bytes\n", size);
    }
  }
  printf("invalid: %d\n", ge::GetSizeByDataType(static_cast<ge::DataType>(999)));
  return 0;
}
```

2. 在 metadef 仓库根目录编译运行：

   ```bash
   g++ -std=c++11 -I inc/external/graph dt_size_demo.cc -o dt_size_demo && ./dt_size_demo
   ```

3. **观察现象**：`types.h` 用 `#include "c_types.h"` 引入 C 枚举，两者同目录，所以只需把 `inc/external/graph` 加入 include 路径即可编译。
4. **预期结果**：依次输出 `4 bytes / 2 bytes / 2 bytes / 8 bytes / 4 bits / -1`（`DT_UNDEFINED` 是 -1），最后一行输出 `-1`（越界返回 -1）。若与你推算不符，回到 4.1.3 的查表逐项核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接把 `DT_INT4` 的大小写成 0.5（字节），或单独提供一个「返回比特」的函数？

**参考答案**：查表函数返回 `int`，无法表达 0.5；而新增一套比特版本函数意味着所有调用方要区分两套 API。用 `1000 + n` 编码后，一个函数、一个返回值即可同时携带「单位」和「数值」，且 1000 远大于任何合法字节数（最大 16），判断无歧义。代价是调用方必须知道这个约定——所以它是 `kDataTypeSizeBitOffset` 公开常量而不是魔法数字。

**练习 2**：若社区提 PR 在 `C_DataType` 的中间位置插入一个新类型，会发生什么？

**参考答案**：`C_DT_MAX` 之前所有后续枚举值整体加 1，`data_type_size` 查找表下标含义错位（例如 `DT_INT16` 查到 reserved 的 -1），并且已编译发布的 ge/算子仓里硬编码的旧数值与新库不一致，直接破坏 ABI。正确做法是追加在 `DT_MAX` 之前末尾，并同步维护查找表。

### 4.2 Format 枚举与位域编码：primary / sub / C0

#### 4.2.1 概念说明

`Format` 回答的问题是：「张量的元素在内存中按什么维度顺序排布」。`FORMAT_NCHW`、`FORMAT_NHWC`、`FORMAT_ND` 是通用格式；`FORMAT_NC1HWC0`、`FORMAT_FRACTAL_Z`、`FORMAT_FRACTAL_NZ` 是昇腾特有的分形格式（为矩阵单元设计）。

本模块真正要掌握的是：**在运行时，一个 `int32_t` 格式值不止是枚举，而是一个位域打包结果**。同一主格式可以叠加不同的 sub-format（子格式，例如某算子变体）和 c0_format（C0 颗粒的 2 的幂次标记）。头文件注释画出了这个布局：

```text
---------------------------------------------
|   4bits  |   4bits   |   2 bytes  | 1 byte |
|----------|-----------|------------|--------|
| reserved | c0_format | sub-format | format |
---------------------------------------------
  bit31..28  bit27..24   bit23..8     bit7..0
```

- 最低 1 字节（bit 0–7）：主格式，即 `Format` 枚举值本身，容量 `0xff` 恰好等于 `C_FORMAT_MAX`。
- 中间 2 字节（bit 8–23）：sub-format。
- 其上 4 比特（bit 24–27）：c0_format，存的不是 C0 数值本身，而是「2 的多少次方」的标记（见 `GetC0Value`）。

#### 4.2.2 核心流程

编码（三段拼装）：

\[
\text{format} = (\text{primary} \,\&\, 0xff) \;|\; ((\text{sub} \,\&\, 0xffff) \ll 8) \;|\; ((\text{c0} \,\&\, 0xf) \ll 24)
\]

解码（按位掩码 + 右移）：

```text
GetPrimaryFormat(f) = (f & 0xff)          -- 取低 8 位
GetSubFormat(f)     = (f & 0xffff00) >> 8 -- 取中间 2 字节
GetC0Format(f)      = (f & 0xf000000) >> 24
GetC0Value(f)       = 1 << (GetC0Format(f) - 1)   -- 还原成真正的 C0 数值
```

`GetC0Value` 把 4 比特还原成 C0 数值的映射是「编码值 k → 数值 \(2^{k-1}\)」。例如编码值 5 对应 C0 = 16（fp16 场景），编码值 6 对应 C0 = 32（int8 场景）。用 4 比特存指数而不是直接存数值，是因为 C0 只会是 2 的幂，存指数更省位。

以单测里的真实数据走一遍：`0x804` = `0x08 << 8 | 0x04`，即主格式 4（`FORMAT_FRACTAL_Z`）、sub-format 8。`GetPrimaryFormat(0x804) == FORMAT_FRACTAL_Z`、`GetSubFormat(0x804) == 8`。

#### 4.2.3 源码精读

- [inc/external/graph/types.h:192-250](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L192-L250)：`Format` 枚举，同样逐项转发 `C_Format`。注意 `FORMAT_FRACTAL_NZ_C0_16/32/2/4/8`（[types.h:243-247](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L243-L247)）——这是「把 C0 直接编进枚举名」的历史路线，与位域 c0_format 是并存的两种表达方式。
- [inc/external/graph/types.h:252-266](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L252-L266)：位域布局注释 + `GetFormatFromSub`。掩码 `0xffU`、移位 `kBitNumOfOneByte`（8），把 sub-format 放到 bit 8 起。
- [inc/external/graph/types.h:268-277](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L268-L277)：`GetFormatFromC0` 与三段合成的 `GetFormatFromSubAndC0`，c0 用 `0xfU` 掩码移 24 位（`kBitThreeBytes`）。
- [inc/external/graph/types.h:279-297](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L279-L297)：解码侧四件套 `GetPrimaryFormat` / `GetSubFormat` / `HasSubFormat` / `HasC0Format` / `GetC0Format`。`HasSubFormat` 的判据是 sub 段大于 0。
- [inc/external/graph/types.h:299-305](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L299-L305)：`GetC0Value`，无 c0 段时返回 -1，否则 \(1 \ll (k-1)\)。
- [tests/ut/base/testcase/types_unittest.cc:90-105](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L90-L105)：官方断言 `GetPrimaryFormat(0x804) == FORMAT_FRACTAL_Z`、`GetSubFormat(0x804) == 8`、`GetSubFormat(0xffffff) == 0xffff`、`GetPrimaryFormat(0xffffff) == 0xff`——注意最后一个说明解码函数对「超范围输入」不做校验，只做位运算，溢出段自然被掩掉。

#### 4.2.4 代码实践

**实践目标**：完成一次手工编码 → 解码的闭环，验证你对位域布局的理解。

1. 新建 `format_bits_demo.cc`（示例代码）：

```cpp
// 示例代码：只依赖 inline 函数，无需链接库
#include <cstdio>
#include "graph/types.h"

int main() {
  // 编码：主格式 FRACTAL_NZ + sub=2 + c0 标记 5
  const int32_t packed = ge::GetFormatFromSubAndC0(ge::FORMAT_FRACTAL_NZ, 2, 5);
  printf("packed = 0x%x\n", packed);
  // 解码
  printf("primary = %d (FRACTAL_NZ = %d)\n",
         ge::GetPrimaryFormat(packed), ge::FORMAT_FRACTAL_NZ);
  printf("sub     = %d\n", ge::GetSubFormat(packed));
  printf("hasC0   = %d, c0code = %d, c0value = %ld\n",
         ge::HasC0Format(packed) ? 1 : 0, ge::GetC0Format(packed), ge::GetC0Value(packed));
  // 对照：纯枚举值解码
  printf("plain ND: primary=%d sub=%d hasSub=%d\n",
         ge::GetPrimaryFormat(ge::FORMAT_ND), ge::GetSubFormat(ge::FORMAT_ND),
         ge::HasSubFormat(ge::FORMAT_ND) ? 1 : 0);
  return 0;
}
```

2. 编译运行：

   ```bash
   g++ -std=c++11 -I inc/external/graph format_bits_demo.cc -o format_bits_demo && ./format_bits_demo
   ```

3. **观察现象**：`packed` 的十六进制形式应能肉眼拆出三段。
4. **预期结果**：`packed = 0x500021e`（c0=5 在 bit24–27，sub=2 在 bit8–23，主格式 `FORMAT_FRACTAL_NZ` = 30 = 0x1e 在低字节）；`primary = 30`、`sub = 2`、`hasC0 = 1`、`c0code = 5`、`c0value = 16`（\(2^{5-1}\)）；`FORMAT_ND` 解码出 `sub=0、hasSub=0`。若 packed 与预期不符，检查你是否算错了移位。**待本地验证**：具体十六进制输出以本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：`GetSubFormat(0xffffff)` 返回多少？这说明解码函数对非法输入是什么态度？

**参考答案**：返回 `0xffff`（见单测 [types_unittest.cc:99](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc#L99)）。解码函数是纯位运算，不做范围校验——合法性由调用方（建图/校验层）保证。这是底层工具函数「快而不管安全」的典型取舍。

**练习 2**：为什么 `FORMAT_FRACTAL_NZ_C0_16` 这类「带 C0 的枚举」和位域 c0_format 会并存？

**参考答案**：`C0_16/32/2/4/8` 是枚举层面的固定组合，历史较早、可读性强，序列化/打印时是一个独立主格式值；位域 c0_format 则允许在同一主格式上动态叠加 C0 信息而不新增枚举项（枚举一发布就不能改，只能追加）。新需求更倾向位域方案，因为不必为每个 C0 变体扩枚举。

### 4.3 从声明到实现：GetSizeInBytes 链路与 pkg_inc 侧的基础类型

#### 4.3.1 概念说明

`GetSizeByDataType` 返回的是「单个元素」的大小，而算子/框架更常问的是「n 个元素共多少字节」——这就是 `GetSizeInBytes(element_count, data_type)`。它与 `GetFormatName` 一样，在 `types.h` 中**只有声明没有函数体**，按 u1-l3 建立的方法，实现要去 `base/` 找：桥接头 `inc/base/type/types_impl.h` 里的 `TypeImpl` 静态类 → `base/type/types_impl.cc`。

这一模块还要快速认识 `pkg_inc/` 下两个「基础类型辅助」头文件，它们是发布包内的公共词汇：

- `def_types.h`：指针与 uint64 互转（设备侧地址在元数据里常以整数携带）。
- `graph_type_utils.h`：`TypeId` 体系——每个 C++ 类型映射到唯一 ID，是下一讲 AnyValue 类型擦除的地基。

#### 4.3.2 核心流程

`GetSizeInBytes` 的完整流程（含比特类型换算）：

```text
输入 element_count, data_type
  ├── element_count < 0                  → 记日志，返回 -1
  ├── TypeUtils::GetDataTypeLength 失败   → 记日志，返回 -1
  ├── 类型大小 > 1000（比特类型）
  │     ├── 乘法溢出检查失败              → 返回 -1
  │     └── 返回 CeilDiv(count * bit数, 8)   -- 向上取整到字节
  └── 普通类型
        ├── 乘法溢出检查失败              → 返回 -1
        └── 返回 count * 字节数
```

注意它没有复用 `types.h` 里的 `GetSizeByDataType` 查表，而是走 `TypeUtils::GetDataTypeLength`（[inc/external/graph/utils/type_utils.h:27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/utils/type_utils.h#L27) 声明，`base/utils/type_utils_impl.cc` 实现）——两套长度表在演进中并存，读代码时要知道这一点。

#### 4.3.3 源码精读

- [inc/external/graph/types.h:186-190](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L186-L190)：`GetSizeInBytes` 的对外声明，「声明无体」→ 说明实现编进了动态库。
- [base/type/types_impl.cc:115-139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L115-L139)：`TypeImpl::GetSizeInBytes` 实现。比特分支在 [types_impl.cc:124-130](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L124-L130)：`type_size - kDataTypeSizeBitOffset` 还原比特数，再 `CeilDiv(count * bits, 8)` 向上取整。所有失败路径都先 `GELOGW` 记日志再返回 -1。
- [base/type/types_impl.cc:83-88](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L83-L88)：`CeilDiv`，经典向上取整 \(\lceil n_1/n_2 \rceil = \lfloor (n_1-1)/n_2 \rfloor + 1\)（n1 ≠ 0 时）。
- [base/type/types_impl.cc:90-113](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L90-L113)：`CheckInt64MulOverflow`，按 a、b 正负号分四种情况用 `INT64_MAX/INT64_MIN` 除法预判溢出。字节数计算是溢出攻击/事故的高发点（shape 来自外部输入），所以先检后乘。
- [base/type/types_impl.cc:19-81](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L19-L81)：`TypeImpl::GetFormatName`，`FORMAT_END` 长度的字符串查找表，越界返回 `"UNKNOWN"`；这就是 `ge::GetFormatName(FORMAT_NCHW)` 返回 `"NCHW"` 的出处。
- [base/type/types_impl.cc:179-181](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L179-L181)：薄封装层——`ge::GetSizeInBytes` 直接转调 `TypeImpl::GetSizeInBytes`。这正是 u1-l3 讲过的「Impl 后缀类 + 薄封装」分层模式的具体一例。
- [pkg_inc/graph/def_types.h:18-50](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/def_types.h#L18-L50)：`PtrToValue`/`ValueToPtr`（指针 ↔ uint64）、`PtrAdd`（带越界保护的指针前进）。设备地址、shared_ptr 跨 ABI 传递时会用到它们。
- [pkg_inc/graph/graph_type_utils.h:36-46](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/graph_type_utils.h#L36-L46)：`GetTypeId<T>()` 返回 `&(TypeIdHolder<PureT>::id)`——用「每个类型独有的静态成员变量的地址」当类型 ID。后续大量 `GetTypeId<std::string>()` 等显式特化声明（[graph_type_utils.h:48-112](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/graph_type_utils.h#L48-L112)）是为了让这些类型 ID 有跨 so 的统一符号定义。现在只需记住机制，细节留给 u2-l3。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「声明 → Impl → 库符号」的链接路径，并验证 `GetSizeInBytes` 的比特换算。

1. 新建 `size_in_bytes_demo.cc`（示例代码）：

```cpp
// 示例代码：GetSizeInBytes 不是 inline，需要链接 libmetadef.so
#include <cstdio>
#include "graph/types.h"

int main() {
  printf("10 x float    = %ld bytes\n", ge::GetSizeInBytes(10, ge::DT_FLOAT));
  printf("10 x int4     = %ld bytes\n", ge::GetSizeInBytes(10, ge::DT_INT4));   // 10*4bit -> 5B
  printf("3  x uint1    = %ld bytes\n", ge::GetSizeInBytes(3, ge::DT_UINT1));   // 3*1bit  -> 1B
  printf("name of NZ    = %s\n", ge::GetFormatName(ge::FORMAT_FRACTAL_NZ));
  printf("bad input     = %ld\n", ge::GetSizeInBytes(-1, ge::DT_FLOAT));
  return 0;
}
```

2. 编译（二选一，取决于你有没有已安装的 CANN）：

   ```bash
   # 方式 A：链接仓库自己构建出的库（先用 build.sh 编译过，路径按实际 build_out 调整）
   g++ -std=c++11 -I inc/external/graph size_in_bytes_demo.cc \
       -L build_out/lib64 -lmetadef -Wl,-rpath,$PWD/build_out/lib64 -o size_in_bytes_demo
   # 方式 B：链接 CANN 安装目录（source 过 set_env.sh 后）
   g++ -std=c++11 -I$ASCEND_HOME_PATH/include/graph size_in_bytes_demo.cc \
       -L$ASCEND_HOME_PATH/lib64 -lmetadef -o size_in_bytes_demo
   ./size_in_bytes_demo
   ```

3. **观察现象**：程序正常输出且无链接错误，说明 `GetSizeInBytes`/`GetFormatName` 的符号确实来自 `libmetadef.so`（可再 `nm -D build_out/lib64/libmetadef.so | grep GetSizeInBytes` 确认，待本地验证）。
4. **预期结果**：`40`、`5`（40 bit 向上取整）、`1`（3 bit 向上取整）、`FRACTAL_NZ`、`-1`。若无法本地构建，可退化为「源码阅读型验证」：把 4.3.2 的伪代码与 [types_impl.cc:115-139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L115-L139) 逐行对照，手算这五个输出。

#### 4.3.5 小练习与答案

**练习 1**：`GetSizeInBytes(1, DT_UNDEFINED)` 返回什么？依据是哪一行？

**参考答案**：返回 -1。`DT_UNDEFINED` 在 `TypeUtils::GetDataTypeLength` 中取长度失败，走到 [types_impl.cc:121-123](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L121-L123) 的失败分支：记 warning 日志并返回 -1。

**练习 2**：为什么 `CeilDiv(10*4, 8)` 是 5 而 `CeilDiv(3*1, 8)` 是 1？

**参考答案**：`CeilDiv(n1, n2) = (n1 - 1)/n2 + 1`（n1≠0）。10 个 int4 元素共 40 bit，\(39/8+1 = 4+1 = 5\) 字节；3 个 uint1 共 3 bit，\(2/8+1 = 0+1 = 1\) 字节。比特类型必须向上取整，否则最后一个不满的字节会被截丢。

**练习 3**：`graph_type_utils.h` 的 `GetTypeId<T>()` 为什么用静态成员地址而不是 `typeid(T)`？

**参考答案**：`typeid` 返回的 `type_info` 名称跨编译器/跨 so 不稳定（且比较依赖实现），而「每个类型独有静态变量的地址」在同一进程内天然唯一、与编译器实现无关，比较就是一次指针相等判断。跨 so 场景下配合显式特化，符号由库统一导出，保证各 so 拿到同一地址。

## 5. 综合实践

**任务：制作一张「类型-格式速查卡」程序。** 把本讲三个模块串成一个可运行的小工具 `meta_cheatsheet.cc`（示例代码）：

1. **DataType 部分**：遍历 `{DT_FLOAT, DT_FLOAT16, DT_BF16, DT_INT4, DT_UINT1, DT_COMPLEX128, DT_STRING}`，用 `GetSizeByDataType` 打印每个类型的大小（区分字节/比特/非法三种输出）。
2. **Format 部分**：对 `GetFormatFromSubAndC0(FORMAT_NC1HWC0, 3, 6)` 的结果依次调用 `GetPrimaryFormat/GetSubFormat/GetC0Format/GetC0Value` 打印解码结果；再对裸枚举 `FORMAT_FRACTAL_NZ` 打印一次解码，对比「带位域」与「裸枚举」的差异。
3. **库函数部分**：用 `GetFormatName` 打印 `{FORMAT_NCHW, FORMAT_NC1HWC0, FORMAT_FRACTAL_NZ, FORMAT_FRACTAL_NZ_C0_16}` 的名字；用 `GetSizeInBytes(100, DT_FLOAT6_E3M2)` 验证 6bit 类型 100 个元素 = 75 字节。

验收标准：输出与你依据 [types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h) 查表推算的结果完全一致；其中第 1、2 部分无需链接库（inline），第 3 部分按 4.3.4 的方式链接 `libmetadef.so`。没有本地编译环境时，完成「源码阅读型」版本：写出每个输出的推算过程，并与 [types_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/types_unittest.cc) 的现有断言互相印证。

## 6. 本讲小结

- `DataType`/`Format` 的 C++ 枚举逐项转发 `c_types.h` 的纯 C 枚举，取值顺序是 ABI 契约，只能尾部追加、不能插入。
- `GetSizeByDataType` 是 inline 查表函数；小于 1 字节的类型用 `kDataTypeSizeBitOffset(1000) + 比特数` 编码大小，返回 -1 表示不定长/非法。
- `Format` 运行时是 32 位位域：低字节主格式、中 2 字节 sub-format、bit24–27 是 c0 编码（`GetC0Value` 按 \(2^{k-1}\) 还原）。
- `GetSizeInBytes` 走「types.h 声明 → TypeImpl 实现 → libmetadef.so 符号」链路，含溢出检查与比特向上取整，是 u1-l3 分层模式的实例。
- `pkg_inc` 的 `def_types.h`（指针/整数互转）与 `graph_type_utils.h`（`GetTypeId<T>` 静态变量地址做类型 ID）是发布包内基础词汇，后者直接服务下一讲的 AnyValue。

## 7. 下一步学习建议

- 下一讲 **u2-l2 AscendString**：为什么这套对外接口宁可自造字符串类也不用 `std::string`——同样是 ABI 主题的延续。
- 之后 **u2-l3 AnyValue 与 TypeId**：本讲 4.3 里埋下的 `GetTypeId<T>()` 伏笔将在那里展开为完整的类型擦除容器。
- 源码延伸阅读：`inc/external/graph/utils/type_utils.h` 与 `base/utils/type_utils_impl.cc`，看 `TypeUtils::GetDataTypeLength` 的第二套长度表以及 DataType/Format 与字符串互转的实现。
