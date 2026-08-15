# u2-l4 Shape、Stride 与 Tensor：张量描述体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 `gert::Shape` 与 `gert::Stride` 的构造、查询与修改接口，理解它们「定长数组 + POD 约束」的设计。
2. 理解 `gert::Tensor` / `gert::TensorV2` 的组成：StorageShape、StorageFormat、DataType、TensorData。
3. 基于 `gert::TensorData` 理解张量数据的所有权管理（manager 回调、placement、Following 布局）。
4. 说清楚 `ge::Tensor`（老图编译体系）与 `gert::Tensor`（exe_graph 运行时体系）的定位差异。

## 2. 前置知识

- **张量（Tensor）**：深度学习中的多维数组。一个张量由两部分描述：「元信息」（形状、数据类型、格式、数据放在哪）和「数据本身」（一块连续或不连续的内存）。
- **Shape（形状）**：每个维度的大小。例如 `{2, 3, 4}` 表示一个 2×3×4 的三维数组，共 24 个元素。
- **Stride（步长）**：沿每个维度移动一格需要跨越的元素个数。对连续的 `{2,3,4}` 张量，stride 是 `{12, 4, 1}`。元素 \((i,j,k)\) 的地址偏移为：

  \[ \text{offset}(i,j,k) = i \cdot s_0 + j \cdot s_1 + k \cdot s_2 \]

  stride 允许描述「非连续」张量（例如切片、转置后的视图），这是 `TensorV2` 存在的意义。
- **POD / standard_layout**：本讲所有 `gert` 结构体都要求「标准布局」，即内存排布与 C 结构体一致、跨编译器跨 so 稳定。这是上一单元反复强调的 ABI 兼容手段（`static_assert(std::is_standard_layout<...>::value)`）。
- **placement（数据位置）**：张量数据在 Host 内存、Device HBM、还是「紧跟在结构体后面」，由枚举 `TensorPlacement` 描述。
- 建议先完成 u2-l1（DataType/Format）与 u2-l3（AnyValue），本讲会直接使用 `ge::DataType`、`ge::Format` 和 `ge::GetSizeInBytes`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/exe_graph/runtime/shape.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h) | `gert::Shape`：定长维度数组，运行时 shape 的原子类型 |
| [inc/external/exe_graph/runtime/stride.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/stride.h) | `gert::Stride`：定长步长数组，与 Shape 同构 |
| [inc/external/exe_graph/runtime/storage_shape.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/storage_shape.h) | `gert::StorageShape`：原始 shape + 运行时 shape 的组合 |
| [inc/external/exe_graph/runtime/tensor_data.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h) | `gert::TensorData`：数据地址、大小、placement 与所有权管理 |
| [inc/external/exe_graph/runtime/runtime_tensor.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h) | `gert::Tensor` 与 `gert::TensorV2`：运行时张量本体 |
| [inc/external/exe_graph/runtime/tensor.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor.h) | 弃用转发头：仅 `#include "runtime_tensor.h"` 并打印迁移警告 |
| [inc/external/graph/tensor.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h) | `ge::Shape` / `ge::TensorDesc` / `ge::Tensor`：老体系张量，全部走 `shared_ptr<Impl>` |
| [tests/ut/base/testcase/tensor_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tensor_unittest.cc) | `TensorV2` 的单元测试，本讲实践的参照模板 |

## 4. 核心概念与源码讲解

本讲的最小模块：**gert::Shape / Stride**、**StorageShape 与 gert::Tensor 本体**、**TensorData 所有权管理**、**ge::Tensor 与 gert::Tensor 对比**。

### 4.1 gert::Shape 与 gert::Stride：定长 POD 的维度描述

#### 4.1.1 概念说明

`Shape` 回答「每个维度多大」。它最大的设计决策是：**不用 `std::vector`，而用定长数组**。原因有二：

1. `std::vector` 内部是指针三元组，跨 so 边界传递时布局不稳定（u2-l2 讲过同样的问题），不符合 ABI 要求。
2. 运行时上下文（下一单元的 `KernelContext`）需要在裸内存上构造、memcpy、跨进程传递，定长 POD 可以整体拷贝。

`Stride` 与 `Shape` 完全同构，只是语义从「维度大小」换成「步长」，两者代码几乎是镜像的。

#### 4.1.2 核心流程

`Shape` 的内存模型：

```text
struct Shape {
  size_t   dim_num_;                  // 有效维度数（0 表示标量）
  int64_t  dims_[25];                 // 定长 dim 数组，最多 25 维
  uint8_t  reserved_[40];             // 预留字段，为未来扩展保留、不破坏布局
};
```

- 构造：`Shape({2, 3, 4})` 走 `initializer_list` 构造，超过 25 维时**静默忽略**（`dim_num_` 保持 0）。
- 查询：`GetDimNum()`、`GetDim(idx)`（越界返回 `kInvalidDimValue` 即 `INT64_MIN`）、`operator[]`（越界**行为未定义**，性能优先）。
- 修改：`SetDim(idx, value)`、`AppendDim(value)`（链式追加）。
- 元素总数：`GetShapeSize()` 把所有 dim 连乘，溢出时返回 `kInvalidDimValue`。

注意 `GetShapeSize` 与 `ge::Shape::GetShapeSize`（4.4 节）的语义差异：gert 版本**不处理 -1/-2（未知维度）**，未知维度会被当作普通整数参与乘法。

#### 4.1.3 源码精读

**最大维数与非法值常量**（[shape.h:L25-L26](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L25-L26)）：定义 `kMaxDimNum = 25` 和 `kInvalidDimValue = INT64_MIN`。25 维上限与 40 字节 `reserved_` 一起决定了 `sizeof(Shape)` 是一个固定常量——这是布局契约的一部分，**不能随意修改**。

**initializer_list 构造**（[shape.h:L40-L49](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L40-L49)）：`Shape({8,3,224,224})` 创建 4 维 shape。注意 L41-L43：超过 `kMaxDimNum` 时直接 `return`，得到一个 0 维（标量）shape，不报错——调用方必须自己保证维数合法。

**元素总数计算**（[shape.h:L111-L119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L111-L119)）：连乘每个 dim，用 `ge::MulOverflow` 做溢出检测，任一步溢出立即返回 `kInvalidDimValue`：

```cpp
int64_t shape_size = 1;
for (size_t i = 0; i < dim_num_; ++i) {
  if (ge::MulOverflow(shape_size, dims_[i], shape_size)) {
    return kInvalidDimValue;
  }
}
```

**拷贝构造只拷有效部分**（[shape.h:L56-L62](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L56-L62)）：注释明确说明「为了提升性能，`dims_` 超过 `dim_num_` 的空间没有拷贝，可能有脏数据」。等价于：

\[ \text{拷贝成本} \propto \text{dim\_num\_} \quad \text{而非} \quad kMaxDimNum \]

**POD 断言**（[shape.h:L214](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L214)）：`static_assert(std::is_standard_layout<Shape>::value, ...)`，编译期强制布局稳定。任何人给 `Shape` 加虚函数或非标准布局成员，metadef 直接编译失败。

**Stride 的镜像实现**（[stride.h:L23-L34](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/stride.h#L23-L34)、[stride.h:L172-L178](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/stride.h#L172-L178)）：`Stride` 的字段（`dim_num_` + `strides_[25]` + `reserved_[40]`）与接口（`GetStride`/`SetStride`/`AppendStride`）与 `Shape` 一一对应，同样有 [stride.h:L185](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/stride.h#L185) 的 POD 断言。可以推断两份代码是同一模板改名生成的——阅读时读懂一份即可。

**一个值得注意的细节**：`SetDim`（[shape.h:L188-L194](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/shape.h#L188-L194)）中 `dim_num_ = (dim_num_ < idx) ? idx : dim_num_`，写第 `idx` 维时只会把 `dim_num_` 抬到 `idx`（而非 `idx + 1`）。实践中推荐用 `AppendDim` 或先 `SetDimNum` 再逐维设置，避免依赖这个边角行为。

#### 4.1.4 代码实践

**实践目标**：验证 `gert::Shape` 的构造、`GetShapeSize` 与溢出行为。

**操作步骤**：

1. 在 `tests/ut/base/testcase/` 下新建 `shape_mytest.cc`（示例代码，非项目原有文件）：

   ```cpp
   #include "exe_graph/runtime/shape.h"
   #include <gtest/gtest.h>

   namespace gert {
   TEST_F(TensorUT, MyShapeBasic) {
     Shape s{2, 3, 4};
     EXPECT_EQ(s.GetDimNum(), 3U);
     EXPECT_EQ(s.GetShapeSize(), 24);          // 2*3*4
     EXPECT_TRUE(s == Shape({2, 3, 4}));

     Shape scalar;
     EXPECT_TRUE(scalar.IsScalar());            // 默认构造是标量
     EXPECT_EQ(scalar.GetShapeSize(), 1);       // 标量连乘结果为 1

     Shape overflow{std::numeric_limits<int64_t>::max(), 2};
     EXPECT_EQ(overflow.GetShapeSize(), Shape::kInvalidDimValue);  // 溢出
   }
   }  // namespace gert
   ```

   注意：本仓库 `ut_metadef` 用 glob 收集 `tests/ut/base/testcase/*.cc`（见 u1-l2），新文件无需改 CMake；`TensorUT` 这个 fixture 名可沿用 `tensor_unittest.cc` 里的定义（同 targets 内多个文件共享符号，若链接冲突则换一个 fixture 名）。

2. 运行 `bash tests/run_test.sh -u`，或只跑该用例：`./build_gcov/ut_metadef --gtest_filter=TensorUT.MyShapeBasic`（具体可执行文件路径以构建输出为准，待本地验证）。

**需要观察的现象**：`GetShapeSize()` 对 `{2,3,4}` 返回 24；对含未知维度 `-1` 的 shape（如 `Shape{2, -1}`）返回 -2（-1 当普通数乘进去），印证「gert 版不处理未知维度」。

**预期结果**：全部断言通过；若把 `Shape{2,-1}` 的断言加上 `EXPECT_EQ(s.GetShapeSize(), -2)` 也能通过。

#### 4.1.5 小练习与答案

**练习 1**：`GetDim(30)` 和 `operator[](30)` 的行为有什么区别？

答案：`GetDim(30)` 检查 `idx >= kMaxDimNum`，越界返回 `kInvalidDimValue`（`INT64_MIN`）；`operator[](30)` 不做检查，直接索引数组，行为未定义（可能读到 `reserved_` 的内容或越界崩溃）。查询路径用 `GetDim`，热路径（已确认合法）用 `operator[]`。

**练习 2**：为什么 `Shape` 的拷贝构造不把 `dims_[25]` 整个数组拷完？

答案：注释写明是性能考虑：只拷 `dim_num_` 个有效元素。`Shape` 在运行时会随上下文大量拷贝（例如整个 `KernelContext` memcpy），25 维数组全拷会浪费最多 25×8 字节的内存带宽；`reserved_` 尾部是否存在脏数据不影响任何正确语义。

**练习 3**：若产品要求支持 30 维张量，直接把 `kMaxDimNum` 改成 30 行不行？

答案：不行。`sizeof(Shape)` 会从当前值变大，所有嵌入了 `Shape` 的结构体（`StorageShape`、`Tensor`、后续的 `KernelContext` 链）布局全部改变，已编译的算子 so 与新框架之间立刻 ABI 不兼容。正确做法是评估后整体升级并保持新旧布局过渡期兼容（这正是 `reserved_` 预留字段存在的意义——优先消耗预留空间而不是扩大结构体）。

### 4.2 StorageShape 与 gert::Tensor：运行时张量本体

#### 4.2.1 概念说明

一个张量的「形状」在 CANN 里有两种视角：

- **origin shape（原始形状）**：用户图里的逻辑形状，如 `{8, 3, 224, 224}`。
- **storage shape（运行时形状）**：数据在 Device 上实际排布的形状。例如 FP16 数据以 `FRACTAL_NZ` 分形排布后，`{8,3,224,224}` 在物理上是 `{16,3,224,224}`。

`gert::StorageShape` 把两者打包；`gert::StorageFormat` 对 format 做同样的事。`gert::Tensor` 再组合 shape、format、datatype 和数据描述（`TensorData`），构成算子执行时看到的完整张量。

`TensorV2` 是 `Tensor` 的超集：额外携带 `Stride` 与 `offset`，用于描述**非连续**张量（视图、切片）；通过 `version_` 标记区分（`kTensorV1` 不带非连续信息，`kTensorV2` 带）。

另外注意一个仓库演进痕迹：老路径 `exe_graph/runtime/tensor.h` 已是弃用转发头（[tensor.h:L13-L21](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor.h#L13-L21)），只做 `#include "runtime_tensor.h"` 并用 `#pragma message` 提示 2027-06 后移除。新代码一律包含 `runtime_tensor.h`。

#### 4.2.2 核心流程

`Tensor` 的构成（[runtime_tensor.h:L332-L339](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L332-L339)）：

```text
class Tensor {                 // sizeof 固定，standard_layout
  StorageShape  storage_shape_;   // origin shape + storage shape
  StorageFormat storage_format_;  // origin format + storage format + 补维规则
  TensorVersion version_;         // V1 / V2
  ge::DataType  data_type_;
  TensorData    tensor_data_;     // 地址/大小/placement/管理回调
  uint8_t       reserved_[...];   // 预留
};
```

非 Following 张量的取数流程：

```text
Tensor::GetData<T>()
  └─> GetAddr()
        └─> tensor_data_.GetAddr()        // 普通情况：直接返回地址（或经 manager 回调换算）
```

Following 张量（数据紧跟结构体）的取数流程：

```text
GetAddr() 看到 placement == kFollowing
  └─> return (uint8_t*)this + sizeof(*this)   // 数据就在对象尾部
```

`TensorV2` 定位元素地址：

\[ \text{addr}(i_0,\dots,i_{n-1}) = \text{base} + \text{offset} + \sum_{k=0}^{n-1} i_k \cdot \text{stride}_k \]

其中 base 由 `GetAddr()` 给出，`offset` 与 `stride_` 是 `TensorV2` 独有字段。

#### 4.2.3 源码精读

**StorageShape 组合两个 Shape**（[storage_shape.h:L27-L30](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/storage_shape.h#L27-L30)）：`StorageShape({8,3,224,224}, {16,3,224,224})` 的写法是「前一个 origin、后一个 storage」，成员见 [storage_shape.h:L90-L93](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/storage_shape.h#L90-L93)（两个 `Shape` + 40 字节预留）。注意 `GetShape()`/`MutableShape()` 返回的是 **origin** shape（[storage_shape.h:L42-L58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/storage_shape.h#L42-L58)），读写运行时 shape 必须用 `GetStorageShape()`/`MutableStorageShape()`，这对接口命名极易混用。

**带数据构造 Tensor**（[runtime_tensor.h:L46-L55](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L46-L55)）：六参构造接收 shape、format、placement、datatype、地址和 manager 回调，并在初始化列表里顺手算好内存大小：`ge::GetSizeInBytes(GetShapeSize(), data_type_)`——这就是 u2-l1 讲过的「声明在 types.h、实现在 libmetadef.so」的字节数计算函数在这里的落点。

**GetAddr 的 Following 分支**（[runtime_tensor.h:L92-L109](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L92-L109)）：placement 为 `kFollowing` 时返回 `this + sizeof(*this)`，即数据紧跟结构体。这服务于「一次分配、头体连续」的内存策略（见 `CreateFollowing`）。

**CreateFollowing 工厂**（[runtime_tensor.h:L159-L174](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L159-L174)）：分配 `sizeof(Tensor) + tensor_size` 的裸内存，头部 placement-new 构造 `Tensor`，数据区不用初始化，placement 设为 `kFollowing`。加法溢出（`ge::AddOverflow`）与分配失败都返回 `nullptr`，全程无异常抛出。

**TensorV2 携带 stride/offset**（[runtime_tensor.h:L366-L372](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L366-L372)）：八参构造比 `Tensor` 多出 `const Stride &stride` 和 `int64_t offset`；访问接口为 [GetStride L627-L629](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L627-L629)、[GetOffset/SetOffset L634-L650](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L634-L650)。成员布局见 [L668-L672](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L668-L672)：内部持有一个完整 `Tensor tensor_` 再外挂 `Stride stride_` 与 `int64_t offset_`。两个类都有 POD 断言（[L343](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L343)、[L674](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L674)）。

**测试里的构造样板**（[tensor_unittest.cc:L15-L36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tensor_unittest.cc#L15-L36)）：`TensorV2 tensor{{{8,3,224,224},{16,3,224,224}}, {ge::FORMAT_ND, ge::FORMAT_FRACTAL_NZ, {}}, kOnDeviceHbm, ge::DT_FLOAT16, nullptr}` ——三层花括号分别对应 StorageShape（内层两个 initializer_list）、StorageFormat（origin/storage/补维）、placement、dtype、地址。这个「花括号嵌套」是运行时张量最典型的字面量写法。

**Set/Get shape 的往返验证**（[tensor_unittest.cc:L81-L90](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tensor_unittest.cc#L81-L90)）：`t2.MutableOriginShape() = Shape{8,3,224,224};` 后 `GetOriginShape()` 取回相等值——「Mutable 拿引用直接赋值、Get 取只读引用」是 gert 体系典型的读写分离模式（写路径不拷贝，读路径不误改）。

**GetSize 与字节数**（[tensor_unittest.cc:L150-L154](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tensor_unittest.cc#L150-L154)）：`StorageShape({1,2,3},{1,2,3})` + `DT_FLOAT` 的张量 `GetSize() == 24`：6 个 float × 4 字节。这个大小是构造时由 `ge::GetSizeInBytes` 算出存进 `TensorData::size_` 的。

#### 4.2.4 代码实践

**实践目标**：构造 `gert::Shape({2,3,4})`，计算元素总数并据此构造张量描述，验证 set/get 往返。

**操作步骤**：

1. 新建 `tests/ut/base/testcase/tensor_mytest.cc`（示例代码）：

   ```cpp
   #include "exe_graph/runtime/runtime_tensor.h"
   #include <gtest/gtest.h>

   namespace gert {
   TEST_F(TensorUT, MyTensorFromShape) {
     // 1. 构造 shape 并计算元素总数
     Shape shape{2, 3, 4};
     const int64_t numel = shape.GetShapeSize();            // 24
     ASSERT_EQ(numel, 24);

     // 2. 据此构造张量（origin 与 storage 一致的简单场景）
     TensorV2 t2{{ {2,3,4}, {2,3,4} },                      // StorageShape
                 { ge::FORMAT_ND, ge::FORMAT_ND, {} },      // StorageFormat
                 kOnHost, ge::DT_FLOAT, nullptr};
     EXPECT_EQ(t2.GetShapeSize(), numel);                   // 张量侧同样得到 24

     // 3. Set/Get shape 往返（参照 SetGetShapeOk_V2 的写法）
     t2.MutableStorageShape() = Shape{2, 3, 4, 6};
     EXPECT_EQ(t2.GetStorageShape(), Shape({2, 3, 4, 6}));
     EXPECT_EQ(t2.GetShapeSize(), 144);                     // 2*3*4*6

     // 4. 字节数 = 元素数 × 类型大小，对照 GetTensorSizeOk_V2
     //    DT_FLOAT 24 个元素应为 96 字节，此处 placement 无地址、size 为 0，
     //    需走带 manager/size 的构造才会计入 size（见 4.3.4）。
   }
   }  // namespace gert
   ```

2. 运行 `bash tests/run_test.sh -u`，过滤 `--gtest_filter=TensorUT.MyTensorFromShape`。

**需要观察的现象**：`GetShapeSize()` 始终取的是 **storage shape** 的连乘（`Tensor::GetShapeSize` 实现 [runtime_tensor.h:L60-L62](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L60-L62)）；改 `MutableStorageShape` 后 shape size 立即变化，而 `GetOriginShape()` 不受影响。

**预期结果**：所有断言通过。若把第 3 步改成写 `MutableOriginShape()`，则 `GetShapeSize()` 不变（origin 不参与 size 计算）——这是初学最容易踩的坑。

#### 4.2.5 小练习与答案

**练习 1**：`GetShape()` / `GetStorageShape()` / `GetOriginShape()` 三个接口分别返回什么？

答案：`GetShape()` 返回整个 `StorageShape`（含 origin 与 storage 两个 `Shape`）；`GetStorageShape()` 只返回其中的运行时 shape；`GetOriginShape()` 只返回原始 shape。`Tensor::GetShapeSize()` 只用 **storage** shape 计算。

**练习 2**：`Tensor` 和 `TensorV2` 应该在什么场景下分别使用？

答案：数据连续排布（stride 就是标准降序连乘）时用 `Tensor`（kTensorV1）；张量是非连续视图（切片、转置、broadcast 出的假维度）时用 `TensorV2`，其 `stride_` 与 `offset_` 描述元素到地址的映射：\(\text{addr} = \text{base} + \text{offset} + \sum i_k s_k\)。`TensorV2` 内嵌 `Tensor` 并由 `friend` 关系直接访问其私有成员。

**练习 3**：为什么 `exe_graph/runtime/tensor.h` 要保留但只做转发？

答案：对外头文件路径本身是 ABI 兼容承诺的一部分，直接删除会让所有 `#include "exe_graph/runtime/tensor.h"` 的下游算子仓编译失败。所以用「转发头 + `#pragma message` 弃用警告 + 计划移除日期（2027-06）」做渐进迁移，这与 u1-l3 讲过的 pkg_inc 壳头文件机制是同一思路。

### 4.3 TensorData：数据所有权与 placement 管理

#### 4.3.1 概念说明

`TensorData` 是张量的「数据半边」：它持有数据地址、字节数、placement，以及一个可选的**管理回调** `TensorAddrManager`。核心问题是所有权：张量数据通常是 Device 上的 HBM 内存，不能 `free` 只能调运行时接口释放，而且这块内存可能被多个张量共享。metadef 的解法是**把释放/共享策略做成函数指针注入**：

- `manager == nullptr`：地址只是「借来看的」，TensorData 不负责释放。
- `manager != nullptr`：所有生命周期操作（取真实地址、释放、共享计数 +1）都通过调用 `manager(addr, operate_type, out)` 完成，具体怎么释放由注入方（如 acl/runtime 适配层）决定。

这与 u2-l3 `AnyValue` 用函数指针 `operate_` 分发运行期操作是同一个套路：**POD 里存不下继承和多态，就用函数指针模拟虚表**。

#### 4.3.2 核心流程

placement 的五种取值（[tensor_data.h:L23-L29](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L23-L29)）：

| placement | 含义 | 归类 |
| --- | --- | --- |
| `kOnDeviceHbm` | Device 的 HBM 内存 | Device |
| `kOnHost` | Host 内存 | Host |
| `kFollowing` | Host 且数据紧跟结构体 | Host |
| `kOnDeviceP2p` | Device 的 P2P 内存 | Device |
| `kTensorPlacementEnd` | 非法/空值哨兵 | — |

`TensorData` 是**只能移动、不能拷贝**的类型：拷贝构造与拷贝赋值被 `= delete`，只有移动构造/移动赋值。析构时自动 `Free()`。所有权操作：

```text
GetAddr()    -> manager 为空直接返回 addr_；否则回调 kGetTensorAddress 换算真实地址
Free()       -> 回调 kFreeTensor，成功后清空 addr_/manager_
ShareFrom()  -> 复制对方三元组，并回调 kPlusShareCount 增加共享计数
Release()    -> 放弃所有权，把 (addr, manager) 交还给调用者，地址不被释放
```

#### 4.3.3 源码精读

**管理回调签名**（[tensor_data.h:L53-L63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L53-L63)）：`TensorOperateType` 定义三种操作 `kGetTensorAddress`/`kFreeTensor`/`kPlusShareCount`，`TensorAddrManager` 是三参数函数指针 `(*)(TensorAddress, TensorOperateType, void**)`。一个回调覆盖三类生命周期请求。

**GetAddr 的换算逻辑**（[tensor_data.h:L114-L124](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L114-L124)）：`manager_ == nullptr || addr_ == nullptr` 时直接返回 `addr_`；否则回调换算，失败返回 `nullptr`。这意味着 `addr_` 未必是数据首地址，可能是一个「句柄」，真实地址要问 manager——这让宿主侧可以把 Device 指针的解码推迟到使用时刻。

**移动语义与析构释放**（[tensor_data.h:L79-L108](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L79-L108)）：移动构造把源对象清空（`addr_=nullptr`、`placement_=kTensorPlacementEnd`），保证同一地址只有一个所有者；移动赋值先 `Free()` 自己再接管；析构函数调用 `Free()`。因此 `Tensor::SetData(TensorData &&)`（[runtime_tensor.h:L85-L87](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L85-L87)）是移动赋值——旧数据若有 manager 会被正确释放。

**共享与放弃所有权**（[tensor_data.h:L190-L226](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L190-L226)）：`ShareFrom` 复制 `addr_/manager_/size_/placement_` 后，若 manager 非空则回调 `kPlusShareCount`——引用计数由注入方维护，TensorData 自己不存计数。`Release(TensorAddrManager &manager)` 把地址和管理函数一起交出去并把自己清空，注释明确「地址没有被释放，调用者负责通过 manager 释放」。

**字段布局**（[tensor_data.h:L228-L234](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L228-L234)）：`addr_` + `manager_` + `size_` + `placement_` + 两个预留字段，同样为将来扩展留了 40+ 字节而保持 sizeof 不变。

**测试验证所有权行为**（[tensor_unittest.cc:L121-L133](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tensor_unittest.cc#L121-L133)）：`TensorData td(a, nullptr)`（无 manager，纯借用）后 `SetData(std::move(td))`，`GetAddr()` 取回 `a`。而 [L156-L162](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/tensor_unittest.cc#L156-L162) 的 `CreateFollowing(32, DT_INT8, total_size)` 验证头体连续分配：`GetSize()==32`。

#### 4.3.4 代码实践

**实践目标**：体会「无 manager 借用」与「Following 布局」两种数据持有方式。

**操作步骤**：

1. 在 `tests/ut/base/testcase/tensor_mytest.cc` 中追加（示例代码）：

   ```cpp
   TEST_F(TensorUT, MyTensorDataOwnership) {
     // a) 借用：manager 为空，TensorData 不负责释放
     static int64_t buf[6] = {0};
     TensorData td(buf, nullptr, sizeof(buf), kOnHost);
     EXPECT_EQ(td.GetAddr(), buf);            // 直接返回原地址
     // td 析构时 manager_ 为空 -> Free() 直接返回 SUCCESS，不会去 free(buf)

     // b) Following：CreateFollowing 一次分配「头 + 数据」
     size_t total = 0;
     auto holder = Tensor::CreateFollowing(24, ge::DT_INT8, total);  // 24 个 int8
     ASSERT_NE(holder, nullptr);
     auto *t = reinterpret_cast<Tensor *>(holder.get());
     EXPECT_EQ(t->GetSize(), 24);
     EXPECT_EQ(t->GetPlacement(), kFollowing);
     EXPECT_EQ(t->GetAddr(), t + 1);          // 数据紧跟结构体
   }
   ```

2. 编译运行方式同 4.2.4。

**需要观察的现象**：a) 中 `GetAddr()` 原样返回栈/静态地址，析构无副作用；b) 中 `GetAddr()` 恰好等于 `t + 1`（对象末尾），且 `total == sizeof(Tensor) + 24`（可以打印 `total` 与 `sizeof(Tensor)` 对照）。

**预期结果**：断言全部通过；`holder`（`unique_ptr<uint8_t[]>`）离开作用域时整块内存一次释放，无需单独管理数据区。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TensorData` 禁止拷贝、只允许移动？

答案：它独占管理一个数据地址（可能带释放回调）。若允许拷贝，两个 TensorData 都持同一 `addr_/manager_`，析构时会对同一地址释放两次（double free）。移动语义保证任意时刻至多一个所有者，`ShareFrom` 则显式走 `kPlusShareCount` 增加引用计数，语义清晰。

**练习 2**：`Free()` 与 `Release()` 的区别是什么？

答案：`Free()` 通过回调 `kFreeTensor` **真正释放**内存并把自身清空；`Release()` **不释放**内存，只是把 `(addr_, manager_)` 的所有权移交给调用者，自身复位为空。前者是「我不要了，销毁它」，后者是「我不要了，给你管」。

**练习 3**：`GetAddr()` 为什么要经过 manager 回调而不是直接返回 `addr_`？

答案：见 [tensor_data.h:L114-L124](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L114-L124) 的注释与实现：当 manager 非空时，`addr_` 可能是宿主侧的句柄而非设备数据首地址（例如延迟映射的 Device 内存），真实地址必须由注入方在取址时刻换算。metadef 作为不依赖 runtime 的底层库，通过回调把这个换算权留给上层。

### 4.4 ge::Tensor 与 gert::Tensor：两套张量体系对比

#### 4.4.1 概念说明

metadef 里有两个 `Tensor`，服务于两个阶段（u1-l1 的 gert/ge 双体系在张量上的投影）：

- **`ge::Shape` / `ge::TensorDesc` / `ge::Tensor`**（`inc/external/graph/tensor.h`）：**图编译阶段**。用户用它在 Host 上建图、给算子描述输入输出。特点：接口丰富（unknown shape、ShapeRange、名字、常量数据、补维规则），但每个对象都是 `shared_ptr<Impl>` 的壳，操作要走 so 边界，有堆分配与间接寻址开销。
- **`gert::Shape` / `gert::Tensor`**（`inc/external/exe_graph/runtime/`）：**执行阶段**。算子 kernel / tiling / 推理函数直接消费。特点：全 POD、定长、可在裸内存上构造和 memcpy，无虚函数无异常，追求零开销。

u1-l4 的示例中两者同框出现过：宿主建图用 `ge::Tensor`，执行侧 `PrepareInputTensors` 转成 `gert` 体系（`kOnDeviceHbm` 等 placement）。

#### 4.4.2 核心流程

两套体系对照表：

| 维度 | `ge::Shape` / `ge::Tensor` | `gert::Shape` / `gert::Tensor` |
| --- | --- | --- |
| 命名空间 / 头文件 | `ge`，`inc/external/graph/tensor.h` | `gert`，`inc/external/exe_graph/runtime/` |
| 阶段 | 图编译、建图 | 执行（kernel/tiling/推理） |
| 存储模型 | 壳类 + `shared_ptr<Impl>`，Impl 在 libmetadef.so 内 | 定长 POD，standard_layout 断言 |
| dim 存储 | `std::vector<int64_t>`（经由 Impl） | 定长 `int64_t dims_[25]` |
| 未知 shape | 原生支持：dim 取 -1/-2、`SetShapeRange` | 不支持：-1 只是被乘的整数 |
| shape 双视角 | `TensorDesc` 分设 `GetShape`/`GetOriginShape` 两组接口 | `StorageShape` 内嵌 origin+storage 两个 `Shape` |
| 数据所有权 | `SetData` 拷贝 / `ResetData` 独占移交，`Clone()` | `TensorData` 移动语义 + manager 回调 + placement |
| 拷贝成本 | 浅拷贝 shared_ptr（引用共享） | 整体 memcpy（值语义，数据不深拷贝） |

#### 4.4.3 源码精读

**ge::Shape 是 Impl 壳**（[graph/tensor.h:L26-L65](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L26-L65)）：唯一数据成员是 `std::shared_ptr<ShapeImpl> impl_`（[L64](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L64)），`ShapeImpl` 前向声明在 [L25](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L25)——这正是 u1-l3 讲过的「对外壳类 + so 内实现」模式。头文件里所有接口都是「声明无体」函数，符号在 libmetadef.so。

**ge::Shape::GetShapeSize 的语义**（[graph/tensor.h:L54-L61](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L54-L61)）：与 gert 版本形成鲜明对比——dim 含 -1/-2 返回 -1（unknown shape）、含 0 返回 0（空张量）、0 维返回 0（**标量算 0**，而 gert 标量连乘结果是 1）、溢出也返回 0。同一名字、不同契约，跨体系迁移代码时必须逐条核对。

**ge::TensorDesc 的编译期特性**（[graph/tensor.h:L68-L144](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L68-L144)）：`SetUnknownDimNumShape()`（[L86](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L86)）、`SetShapeRange`（[L88](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L88)）、常量数据 `SetConstData/GetConstData`（[L122-L123](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L122-L123)）、补维规则（[L124-L137](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L124-L137)）。这些概念在 gert 侧要么没有（ShapeRange 在 exe_graph 中由独立的 `Range` 类型承担，见 u3-l4），要么收敛为 POD 字段（补维规则变成 `StorageFormat` 里的 `ExpandDimsType`）。

**ge::Tensor 的数据接口与所有权提示**（[graph/tensor.h:L160-L174](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L160-L174)）：一族 `SetData` 重载加 `ResetData`，[L216-L217](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L216-L217) 的注释点破差异：`ResetData(uint8_t*, size, deleter)` 是「高性能接口，与 SetData 的区别是避免重复 make_shared，此时需要用户保证该 tensor 的内存只被当前 tensor 使用，具有独占所有权」——即常规 `SetData` 走共享指针包装（存在分配与共享计数开销）。这回答了 u1-l4 遗留的问题方向：ge::Tensor 的数据管理建立在 shared_ptr 之上，而非 gert 的「回调 + 移动」模型。`Clone()`（[L219](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L219)）提供显式深拷贝出口。

**为什么执行侧不能用 Impl 壳**：gert 张量会被放进 `KernelContext`（下一单元），随上下文整块 memcpy、跨 so 传递、甚至在 Device 侧内存上构造。`shared_ptr` 的控制块布局不稳定、引用计数跨 so 不安全，全 POD 的值语义才是可行解。这个「编译期壳 / 执行期 POD」的二分是贯穿 metadef 的架构主线。

#### 4.4.4 代码实践

**实践目标**：用同一份 `{2,3,4}` 分别走 ge 与 gert 两条路，对照行为差异。

**操作步骤**（示例代码，建议追加到 `tests/ut/base/testcase/tensor_mytest.cc`）：

```cpp
#include "graph/tensor.h"

TEST_F(TensorUT, GeVsGertShapeSize) {
  // ge 体系：编译期描述
  ge::Shape ge_shape(std::vector<int64_t>{2, 3, 4});
  EXPECT_EQ(ge_shape.GetShapeSize(), 24);
  ge::TensorDesc desc(ge_shape, ge::FORMAT_ND, ge::DT_FLOAT);
  desc.SetShape(ge_shape);
  EXPECT_EQ(desc.GetShape().GetShapeSize(), 24);

  // 未知 shape：ge 能表达
  ge::Shape unknown(std::vector<int64_t>{2, -1});
  EXPECT_EQ(unknown.GetShapeSize(), -1);      // unknown -> -1

  // gert 体系：执行期描述
  gert::Shape gert_shape{2, 3, 4};
  EXPECT_EQ(gert_shape.GetShapeSize(), 24);
  gert::Shape gert_unknown{2, -1};
  EXPECT_EQ(gert_unknown.GetShapeSize(), -2); // -1 被当普通数乘：2 * (-1)
}
```

链接依赖说明：`ge::Shape/TensorDesc` 的实现符号在 `libmetadef.so`，本仓库 `ut_metadef` 已链接该库（u1-l2 的四个产物目标），因此放在 `tests/ut/base/testcase/` 下可直接编译。

**需要观察的现象**：同为「{2,-1} 的 shape size」，ge 返回 -1（显式 unknown 语义），gert 返回 -2（纯算术乘积）。这就是两套体系契约差异的最小反例。

**预期结果**：断言全部通过（其中 gert 侧 `-2` 与 ge 侧 `-1` 的对照是本实践的核心观察点）。运行命令：`bash tests/run_test.sh -u` 后过滤 `TensorUT.GeVsGertShapeSize`，具体输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ge::Tensor` 每个方法都要跨 so 调用，而 `gert::Tensor` 不用？

答案：`ge::Tensor` 是壳类，真实数据在 `shared_ptr<TensorImpl>` 指向的、定义于 libmetadef.so 内部的 Impl 对象里，任何 getter/setter 都要解引用并调用 so 内符号；`gert::Tensor` 是全 POD，所有成员（Shape、Format、TensorData）都在对象本体内，头文件里的 inline 方法直接操作字段，无需跨 so。

**练习 2**：把一个 `gert::Tensor` 深拷贝十份，数据会被拷十份吗？

答案：不会。`gert::Tensor` 的拷贝是 POD 值拷贝：`StorageShape/Format/DataType` 逐字段复制，`TensorData` 部分随整体 memcpy 复制其 `addr_/manager_` 等字段（注意：直接 memcpy 会绕过 TensorData 的移动语义，产生两个指向同一地址的描述——真正安全的共享要走 `ShareFrom` 增加 manager 侧计数）。数据本体从不深拷贝，深拷贝语义由持有方通过 manager 显式完成。

**练习 3**：执行阶段如何表达「shape 的某个维度范围未知」？

答案：不在 `gert::Shape` 里塞 -1/-2（它不支持），而是用 exe_graph 独立的 `Range`/`ShapeRange` 机制，在 `InferShapeRangeContext` 等推理上下文中推导（本手册 u3-l4 专门讲解）。gert::Shape 只描述已经确定的执行期形状。

## 5. 综合实践

**任务：实现一个「shape 合法性检查 + 字节数计算」的小工具函数，并写单测覆盖 ge/gert 双体系。**

要求：

1. 写一个自由函数 `CheckAndBytes(const gert::Shape &s, ge::DataType dt, int64_t &bytes)`：
   - `s.GetShapeSize()` 返回 `kInvalidDimValue` 时返回 `ge::GRAPH_FAILED`；
   - 否则用 `ge::GetSizeInBytes(numel, dt)` 计算字节数写入出参，返回 `ge::GRAPH_SUCCESS`（`GetSizeInBytes` 的声明与语义回顾 u2-l1；`ge::graphStatus` 定义见 [inc/external/graph/error_codes.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/error_codes.h)）。
2. 为它写 gtest 用例，至少覆盖：`{2,3,4}+DT_FLOAT`（期望 96 字节）、`{2,3,4}+DT_FLOAT16`（期望 48 字节）、标量 shape + `DT_INT32`、溢出 shape（如两个 `INT64_MAX` 维）期望失败。
3. 再写一个对照用例：同样的 `{2,3,4}` 用 `ge::TensorDesc(ge::Shape(...), FORMAT_ND, DT_FLOAT)` 表达，并断言 `desc.GetShape().GetShapeSize() == 24`，体会同一信息在两套体系下的构造方式差异。
4. 把上述代码放入 `tests/ut/base/testcase/tensor_mytool_ut.cc`，用 `bash tests/run_test.sh -u` 跑通（构建细节见 u1-l2；gtest 目标按 glob 自动收集新文件）。

这个任务串起了本讲全部要点：gert::Shape 的构造与溢出语义、GetSizeInBytes 的跨界调用（u2-l1）、ge::TensorDesc 的编译期用法，以及 metadef 单测的组织方式（u1-l2）。

## 6. 本讲小结

- `gert::Shape`/`gert::Stride` 是定长（25 维上限）POD，拷贝只复制有效 dim，`GetShapeSize()` 连乘溢出返回 `INT64_MIN`，且**不处理未知维度**（-1/-2 只是普通数）。
- `gert::Tensor` = StorageShape（origin+storage 双 shape）+ StorageFormat + DataType + TensorData + 预留字段，整体 standard_layout；`TensorV2` 额外携带 `Stride` 与 `offset`，用 \(\text{base}+\text{offset}+\sum i_k s_k\) 定位非连续张量元素。
- `TensorData` 用「函数指针 manager + 三种操作码」在 POD 里模拟多态，完成取址/释放/共享三类所有权操作；只移动不拷贝，析构自动 `Free()`；`kFollowing` placement 表示数据紧跟结构体（`CreateFollowing` 一次分配头体连续内存）。
- `ge::Tensor`（编译期，`shared_ptr<Impl>` 壳，支持 unknown shape/ShapeRange/常量数据）与 `gert::Tensor`（执行期，全 POD 值语义）是同一事物在两个阶段的两种投影，接口同名处契约不同（如 `GetShapeSize` 对标量分别返回 0 与 1）。
- 老 include 路径 `exe_graph/runtime/tensor.h` 已是弃用转发头（2027-06 后移除），新代码一律用 `runtime_tensor.h`。

## 7. 下一步学习建议

本讲结束后，你已经掌握了 gert 体系全部「数据词汇」：Shape、Stride、Tensor、TensorData。下一讲 **u3-l1 KernelContext 与 Chain** 将把这些类型组装成算子执行时的上下文——你会看到 `gert::Tensor` 如何被放进 `KernelContext` 的 Chain 链式结构、通过 `NodeDefId` 随机访问输入输出，以及为什么整条链必须保持 POD。建议提前浏览 [inc/external/exe_graph/runtime/kernel_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h) 的开头 100 行，找到 `Shape`/`Tensor` 出现的位置，带着「它们在上下文里如何排布」的问题进入下一讲。
