# TilingContext：算子 Tiling 阶段的上下文

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 tiling 阶段在算子执行流程中的位置，以及 TilingContext 在框架与算子 TilingFunc 之间承担的「读输入、写结果」角色。
2. 熟练使用 `GetInputShape`、`GetInputTensor`、`GetOutputShape`、`GetAttrs`、`GetPlatformInfo`、`GetCompileInfo` 等接口，并理解它们背后统一的「槽位（values 数组）+ 指针解释」机制。
3. 掌握 tiling 结果写出的全套接口：`SetTilingKey`、`SetSimdNumBlocks`、`GetTilingData<T>`、`GetWorkspaceSizes`、`SetSimtBlockDim` 等，理解 `TilingOutputIndex` 枚举定义的输出槽位顺序。
4. 理解 TilingContext 为什么必须是 POD（`static_assert(std::is_standard_layout<...>)`），以及「零新增数据成员、纯类型化视图」的设计手法。
5. 了解 TilingParseContext 的使用场景：算子编译信息（json）在执行期如何被反序列化回内存结构。

## 2. 前置知识

- **什么是 tiling**：昇腾芯片上有大量 AI Core，一个大的计算任务需要被切成小块分给各个核。算子执行前，框架会在 Host 侧调用算子注册的 **TilingFunc**，根据输入 shape、平台信息等计算出切分参数（每个核处理多少数据、开多少个 block、需要多大 workspace 等）。这个过程叫 tiling，产出的参数叫 **tiling data**。
- **上下文（Context）模式**：TilingFunc 的签名是 `ge::graphStatus TilingFunc(gert::TilingContext *context)`。框架不逐个传参数，而是把所有输入信息（shape、tensor、属性、平台）和所有输出槽位（tiling key、block dim、tiling data……）打包进一块连续内存，以 `TilingContext` 的视角交给算子。算子从 context「读」输入、「写」结果。
- **本讲依赖上一讲（u3-l2）的结论**：`KernelRunContext` 是变长纯 C 结构体（头部计数 + `values` 柔性指针数组，槽序列输入在前、输出在后）；`ExtendedKernelContext` 以 protected 继承 + `static_cast` 提供类型化访问，**零新增数据成员**。本讲的 `TilingContext` 沿用完全相同的手法再叠一层。
- **Chain 值槽**：`values` 数组每个元素指向一个 `Chain`（16 字节类型擦除值槽）。`sizeof(T) <= 8` 的数据内联存在 Chain 里，大对象在 Chain 里存指针。参见 [kernel_context.h:L16-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L16-L54)。
- **失败语义**：整个 exe_graph 体系不抛异常，所有 Get 接口失败时返回空指针或哨兵值（如 `std::numeric_limits<uint32_t>::max()`），Set 接口失败返回 `ge::GRAPH_FAILED`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `inc/external/exe_graph/runtime/tiling_context.h` | 本讲主角。`gert::TilingContext` 类，约 840 行，是 metadef 中最大的单个对外头文件之一，定义了 tiling 阶段的全部读写接口 |
| `inc/external/exe_graph/runtime/tiling_parse_context.h` | `gert::TilingParseContext` 类，编译期 json 信息在执行期反序列化的上下文，仅 3 个接口 |
| `inc/external/exe_graph/runtime/compute_node_info.h` | `ComputeNodeInfo` / `CompileTimeTensorDesc` / `AnchorInstanceInfo`，描述计算节点（IR 输入输出个数、实例化信息、属性），是 TilingContext 多个接口定位槽位的依据 |
| `inc/external/exe_graph/runtime/extended_kernel_context.h` | 父类 `ExtendedKernelContext`，提供 `GetComputeNodeInfo`、`GetAttrs`、`GetDynamicInputPointer` 等被 TilingContext 复用的能力 |
| `inc/external/exe_graph/runtime/kernel_context.h` | 祖先类 `KernelContext`，提供最终的槽位访问原语 `GetInput`/`GetInputPointer`/`GetOutputPointer` |
| `inc/external/exe_graph/runtime/tiling_data.h` | `gert::TilingData` 类，tiling 结果的数据载体（capacity/data_size/data 三字段 + Append/Expand） |
| `inc/external/exe_graph/runtime/runtime_tensor.h` | `gert::Tensor` / `TensorV2`，输入槽位中实际存放的张量对象 |
| `docs/zh/api/c_api/gert_TilingContextBuilder_SetSimtBlockDim.md` | C 接口文档示例，展示宿主侧如何用 Builder 设置 SIMT Block 维度 |
| `tests/ut/register/testcase/tiling_register_unittest.cc` | tiling data 定义宏（`BEGIN_TILING_DATA_DEF` 等）的单元测试，本讲实践会参考 |

## 4. 核心概念与源码讲解

### 4.1 TilingContext 的定位与 POD 骨架

#### 4.1.1 概念说明

`TilingContext` 是「tiling kernel 的 context」——框架调用算子 TilingFunc 时传入的唯一参数对象。它要解决的问题是：tiling 阶段需要的信息种类非常多（每个输入的 shape 与数据、算子属性、平台信息、编译期缓存信息），产出的结果也非常多（tiling key、block 维度、tiling data、workspace……），如果逐个传参，接口会随着需求不断膨胀且破坏 ABI。metadef 的方案是：**所有东西都放进一块按约定排列的内存里，TilingContext 只是这块内存的类型化视图**。

关键设计约束：这块内存由框架侧分配，算子侧（可能是另一个独立编译的 so）只拿指针使用，两侧的编译环境可能不同。因此 TilingContext 必须保持**标准布局（standard layout）**，且**不能新增任何数据成员**——否则一旦头文件版本不一致，内存解释就会错位。这正是文件末尾那行 `static_assert` 存在的意义。

#### 4.1.2 核心流程

一次 tiling 调用的完整流程：

```text
框架侧（宿主）                                算子侧
────────────                                ──────
1. 按 tiling 内存排布约定分配一块连续内存
   [输入 shape/tensor 槽位 | 输出 shape 槽位 |
    compile_info | platform | tiling_func |
    deterministic | ... | tiling 输出槽位]
2. 用 Builder 逐槽位填充数据
3. reinterpret_cast 换成 TilingContext 视角
4. 调用算子注册的 TilingFunc(context) ──────► 5. 从 context 读输入
                                                （GetInputShape/GetAttrs/...）
                                             6. 计算切分方案
                                             7. 向 context 写结果
                                                （SetTilingKey/GetTilingData/...）
8. 读取 tiling 输出槽位，驱动后续编译/执行 ◄────
```

#### 4.1.3 源码精读

类的声明只有一行继承，且整个 800 多行的类体里**全部是成员函数，没有一个数据成员**：

[tiling_context.h:L31-L34](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L31-L34) —— 注释「tiling kernel的context」与类声明 `class TilingContext : public ExtendedKernelContext`。继承链是 `TilingContext → ExtendedKernelContext →(protected) KernelContext → KernelRunContext`，每一层都零新增成员，因此 `sizeof(TilingContext) == sizeof(KernelRunContext)`。

文件末尾把布局契约固化为编译期断言：

[tiling_context.h:L840](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L840) —— `static_assert(std::is_standard_layout<TilingContext>::value, ...)`。任何人给这个类加了虚函数或非标准布局成员，编译直接失败。

父类提供的「计算节点信息」入口是多数接口的第一步：

[extended_kernel_context.h:L166-L168](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L166-L168) —— `GetComputeNodeInfo()` 把 `KernelRunContext` 头部的 `compute_node_info` 指针 `static_cast` 成 `const ComputeNodeInfo *`。TilingContext 的几乎所有公开接口第一行都是取它做边界检查。

头文件顶部还有一个配套的小工具类 `Dim3`：

[tiling_context.h:L25-L29](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L25-L29) —— 三个 `uint32_t` 成员 x/y/z 构成的三维向量，用于 SIMT block/grid 维度，后两个参数默认为 1。

#### 4.1.4 代码实践

**实践目标**：用编译器验证「TilingContext 与底层结构体共享布局」这一论断。

1. 写一个包含 `exe_graph/runtime/tiling_context.h` 的 cpp 文件（示例代码，非项目原有代码）：

```cpp
#include <cstdio>
#include <type_traits>
#include "exe_graph/runtime/tiling_context.h"

int main() {
  printf("sizeof(TilingContext)      = %zu\n", sizeof(gert::TilingContext));
  printf("sizeof(ExtendedKernelContext) = %zu\n", sizeof(gert::ExtendedKernelContext));
  printf("sizeof(KernelContext)      = %zu\n", sizeof(gert::KernelContext));
  printf("is_standard_layout = %d\n",
         std::is_standard_layout<gert::TilingContext>::value);
  return 0;
}
```

2. 编译时需要 metadef 的头文件路径（可参考 `build.sh` 中 cmake 注入的 include 路径，或直接用 `tests/ut` 的编译环境）。
3. **观察现象**：三个 sizeof 应完全相等（等于 `KernelRunContext` 的大小），`is_standard_layout` 为 1。
4. **预期结果**：数值上印证「零新增成员、纯视图」的设计。具体数值取决于平台指针大小，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TilingContext 不能像普通 C++ 类那样加一个 `std::vector<int> members_` 成员来缓存中间结果？

**答案**：`std::vector` 不是标准布局友好的跨 ABI 类型（其内部布局随标准库版本和 `_GLIBCXX_USE_CXX11_ABI` 变化，参见 u2-l2 对 AscendString 的讲解），加入后 `is_standard_layout` 断言失败；更重要的是，这块内存由框架侧分配、算子侧解释，两侧头文件版本可能不同，任何新增成员都会移动后续槽位的偏移，直接破坏 ABI。算子侧的中间结果应放在 TilingFunc 的栈上，最后通过输出接口写出。

**练习 2**：`GetComputeNodeInfo()` 返回空指针时，TilingContext 的接口会怎样表现？

**答案**：以 `GetInputShape` 为例（tiling_context.h L63-73），第一步就检查 `compute_node_info == nullptr` 并返回 `nullptr`；`GetPlatformInfo`、`GetDeterministic` 等同样如此（后者返回 `int32_t` 最大值作哨兵）。即整条链路保持「空值失败、不崩溃」语义。

---

### 4.2 读取输入与输出：Shape、Tensor 与动态输入

#### 4.2.1 概念说明

tiling 的第一件事是「看清输入长什么样」。TilingContext 提供三组读取接口：

1. **按物理槽位访问**：`GetInputShape(index)` / `GetInputTensor(index)` / `GetOutputShape(index)`——index 是实例化后的物理序号（一个 `DYNAMIC_INPUT` 实例化了 3 个输入，就占 3 个槽位）。
2. **按 IR 原型访问**：`GetRequiredInputShape(ir_index)` / `GetOptionalInputTensor(ir_index)` / `GetDynamicInputShape(ir_index, relative_index)`——ir_index 是算子 IR 原型定义中的序号，框架经 `AnchorInstanceInfo` 把它翻译成物理槽位（u3-l2 已讲过这套翻译机制）。
3. **非连续张量（view）信息**：`InputIsView` / `GetInputStride` / `GetInputOffset`——当输入是某个大张量的切片时，tiling 需要知道 stride 和 offset 才能正确切分。

一个值得注意的细节：`GetInputShape` 和 `GetInputTensor` 读的是**同一个槽位**，只是用不同类型解释。

#### 4.2.2 核心流程

以 `GetInputShape(0)` 为例的调用链：

```text
TilingContext::GetInputShape(0)                    // 边界检查：index < inputs_num
  └─ KernelContext::GetInputPointer<StorageShape>(0)   // 取 values[0] 指向的 Chain
       └─ KernelContext::GetInput(0)                    // 越界返回 nullptr
            └─ Chain::GetPointer<StorageShape>()        // sizeof > 8 → 返回 data.pointer
```

按 IR 序号访问则多一步翻译：

```text
TilingContext::GetDynamicInputShape(ir, rel)
  └─ ExtendedKernelContext::GetDynamicInputPointer<StorageShape>(ir, rel)
       ├─ GetIrInputInstanceInfo(ir)      // 从 ComputeNodeInfo 取 AnchorInstanceInfo
       ├─ 检查 GetInstanceNum() > rel
       └─ GetInputPointer(instance_start + rel)   // 翻译成物理槽位
```

#### 4.2.3 源码精读

按物理槽位取输入 shape，带完整的两级判空：

[tiling_context.h:L63-L73](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L63-L73) —— `GetInputShape`：先确认 `compute_node_info` 非空、`index < GetInputsNum()`，再 `GetInputPointer<StorageShape>(index)`。注释明确说明返回的 shape「包含了原始shape与运行时shape」（即 StorageShape 的 origin/storage 双视角，参见 u2-l4）。

同一槽位的另一个视角：

[tiling_context.h:L80-L82](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L80-L82) —— `GetInputTensor` 直接 `GetInputPointer<Tensor>(index)`，连边界检查都省了（Chain 内部解释失败只会得到空数据指针）。注释里的关键信息：**只有算子被配置为 tiling 数据依赖时，返回的 Tensor 才保存 Host 内存地址；否则地址为 nullptr**——框架默认不会为了 tiling 把设备数据搬回 Host。

为什么同一指针既能当 `StorageShape*` 又能当 `Tensor*` 用？看 Tensor 的成员布局：

[runtime_tensor.h:L333](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L333) —— `StorageShape storage_shape_;` 是 `Tensor` 的**第一个私有成员**。由于两个类型都远大于 8 字节，Chain 走「存指针」分支（[kernel_context.h:L33-L36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L33-L36)），两个视角拿到的是同一个地址；`StorageShape` 恰好位于 `Tensor` 起始处，所以 shape 视图是安全的。这是「布局约定即接口契约」的又一处体现。

按 IR 序号访问的三件套（REQUIRED/OPTIONAL 是 DYNAMIC 取第 0 个实例的语法糖，与 u3-l2 的结论一致）：

[tiling_context.h:L100-L110](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L100-L110) —— `GetDynamicInputTensor(ir_index, relative_index)` 转发 `GetDynamicInputPointer<Tensor>(ir_index, relative_index)`；`GetOptionalInputShape(ir_index)` 转发 `GetDynamicInputPointer<StorageShape>(ir_index, 0)`。

[extended_kernel_context.h:L200-L210](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L200-L210) —— `GetDynamicInputPointer` 的翻译实现：取实例化信息、检查 relative index 合法、用 `GetInstanceStart() + relative_index` 定位物理槽位。

输出 shape 复用输入槽序列（tiling 上下文里「输出 shape」是框架预先推导好喂进来的，不是算子写的）：

[tiling_context.h:L143-L155](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L143-L155) —— `GetOutputShape`：`offset = GetInputsNum()`，然后 `GetInputPointer<StorageShape>(offset + index)`。即**输出 shape 紧跟在所有输入之后排列在同一序列里**，这与 u3-l1 讲的「输入在前、输出在后扁平排列」完全吻合。

非连续张量信息，先看私有的统一入口：

[tiling_context.h:L799-L815](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L799-L815) —— `GetInputViewTensor`：以 `TensorV2` 视角取槽位，若 `GetVersion() == kTensorV1`（老版本张量不携带 stride/offset）则返回 nullptr。公开接口 `InputIsView` / `GetInputStride` / `GetInputOffset`（[L588-L620](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L588-L620)）都基于它实现，view 不存在时分别返回 `false` / `nullptr` / `-1`。

#### 4.2.4 代码实践

**实践目标**：用源码阅读方式验证「输出 shape 排在输入 shape 之后」。

1. 打开 [kernel_context.h:L273-L294](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L273-L294)，阅读 `GetOutputPointer`：它取 `context_.values[context_.input_size + i]`。
2. 对比 `TilingContext::GetOutputShape`（L143-155）：它用的是 `GetInputPointer(offset + index)` 而不是 `GetOutputPointer`。
3. **观察现象**：两者下标公式一致，说明 tiling 上下文中「输出 shape」确实占用输入侧槽序列的尾部；`GetOutputPointer` 留给了 4.4 节的 tiling 结果输出。
4. **预期结果**：你能向别人解释清楚为什么 `GetOutputShape` 要绕道 `GetInputPointer`——因为 KernelRunContext 的 `output_size` 计数属于 tiling 结果槽位，而输出 shape 被框架排进了 input 段。这是一个纯阅读实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：算子 IR 原型定义了 1 个 REQUIRED_INPUT `x` 和 1 个 DYNAMIC_INPUT `xs`（实例化为 3 个）。tiling 函数里想拿 `xs` 的第 2 个实例的 shape，该怎么调用？`x` 呢？

**答案**：`xs` 第 2 个实例：`context->GetDynamicInputShape(1, 1)`（ir_index=1 是 `xs`，relative_index=1 是第 2 个，有效范围 [0,2]）。`x`：`context->GetRequiredInputShape(0)`，它等价于 `GetDynamicInputPointer<StorageShape>(0, 0)`（L125-127）。

**练习 2**：`GetInputTensor` 返回的 Tensor 里 `GetData()` 是空指针，最可能的原因是什么？

**答案**：算子没有配置 tiling 数据依赖。注释（L76）写明：只有配置了 tiling 数据依赖，Tensor 中才保存 Host 内存地址，否则为 nullptr。框架不会默认为 tiling 阶段做设备到主机的数据搬运。

**练习 3**：`GetInputStride(index)` 对一个普通连续张量返回什么？为什么？

**答案**：返回 `nullptr`。`GetInputViewTensor`（L799-815）把槽位解释为 `TensorV2`，普通连续张量要么版本是 kTensorV1、要么未携带非连续描述信息，统一返回 nullptr，于是 `GetInputStride` 走空指针分支。

---

### 4.3 隐藏输入槽位：CompileInfo、PlatformInfo 与确定性计算

#### 4.3.1 概念说明

除了逐个输入/输出的 shape，tiling 还需要三类「全局信息」：

- **CompileInfo（编译信息）**：算子离线编译阶段（如 autotune）产生的缓存数据，例如该算子在某 shape 档位下的最优切分方案。执行期直接复用，避免重复计算。
- **PlatformInfo（平台信息）**：`fe::PlatFormInfos` 指针，描述当前芯片的核数、内存层级等硬件特征。tiling 必须知道「有几个核」才能决定切几份。
- **Deterministic / DeterministicLevel（确定性计算开关）**：某些场景要求算子执行结果可复现（如精度对齐），tiling 时需要读取该开关选择确定性调度。

这三类信息**不在 ComputeNodeInfo 里，也没有专门的成员变量**，而是悄悄追加在输入 shape 槽序列的尾部。源码用「index + 偏移」的算式定位它们，排布约定只写在注释里——这是阅读本文件时最容易踩坑的地方。

#### 4.3.2 核心流程

tiling 上下文输入段的实际排布（由各接口的定位算式反推汇总）：

```text
values[0 .. inputs_num-1]                      输入 shape/tensor
values[inputs_num .. inputs_num+outputs_num-1] 输出 shape
values[in+out+0]                               compile_info（void* 指针）
values[in+out+1]                               platform（fe::PlatFormInfos*）
values[in+out+2]                               tiling_func（注释提及）
values[in+out+3]                               deterministic（int32）
values[in+out+4]                               deterministic level（int32）
```

#### 4.3.3 源码精读

CompileInfo 的定位算式：

[tiling_context.h:L36-L48](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L36-L48) —— `GetCompileInfo()`：`index = GetInputsNum() + GetOutputsNum()`，取 `GetInput(index)` 的 Chain，再 `GetValue<void *>()` 拿出内联存的指针。模板重载（[L54-L57](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L54-L57)）把它 `reinterpret_cast` 成用户类型 `const T*`。

PlatformInfo 用 index+1：

[tiling_context.h:L443-L455](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L443-L455) —— `GetPlatformInfo()`：`GetInput(index + 1U)` 后 `GetValue<fe::PlatFormInfos *>()`。注意 `fe::PlatFormInfos` 只在前向声明（[L20-L22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L20-L22)），metadef 不依赖 fe 库，只透传指针——依赖方向严格单向的又一例证。

Deterministic 用 index+3，且注释直接写出了排布顺序：

[tiling_context.h:L461-L474](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L461-L474) —— `GetDeterministic()`：注释「按照tiling内存排布，将确定性计算的字段添加在 inputshape outputshape compileinfo platform tiling_func之后」，即槽位 `index + 3U`。失败返回 `int32_t` 最大值。[L480-L493](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L480-L493) 的 `GetDeterministicLevel` 同理取 `index + 4U`。

这些算式依赖的 `GetInputsNum()` / `GetOutputsNum()` 来自 ComputeNodeInfo：

[compute_node_info.h:L199-L208](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/compute_node_info.h#L199-L208) —— 返回计算节点实例化后的输入/输出个数（区别于 L185-194 的 `GetIrInputsNum`/`GetIrOutputsNum` 返回 IR 原型个数）。

算子属性也经由 ComputeNodeInfo 获得（继承自父类，非 TilingContext 新增）：

[extended_kernel_context.h:L133-L139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L133-L139) —— `GetAttrs()` 返回 `const RuntimeAttrs *`，注释说明「仅IR原型中定义的属性可被获取到」。ComputeNodeInfo 本身是一个变长结构：头部计数之后跟着 `place_holder`，按「输入实例化信息 | 输入输出 TensorDesc | RuntimeAttrs | 输出实例化信息」排列（[compute_node_info.h:L338-L340](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/compute_node_info.h#L338-L340) 的注释）。

#### 4.3.4 代码实践

**实践目标**：亲手排布一张 tiling 上下文输入段内存图。

1. 逐个阅读 `GetCompileInfo`（L36-48）、`GetPlatformInfo`（L443-455）、`GetDeterministic`（L461-474）、`GetDeterministicLevel`（L480-493）中 `GetInput(...)` 的下标表达式。
2. 假设某算子实例化后 `inputs_num = 3`、`outputs_num = 1`，写出每个隐藏槽位的绝对下标。
3. **预期结果**：compile_info=values[4]、platform=values[5]、tiling_func=values[6]、deterministic=values[7]、deterministic_level=values[8]。
4. 思考题自测：如果未来要新增一个「隐藏输入」，源码维护者应该加在哪个下标？为什么 `GetDeterministic` 的注释特意强调排列顺序？（答案：只能追加在尾部（index+5），中间插入会让旧版本算子 so 读错槽位，破坏 ABI。）

这是源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`GetCompileInfo<T>()` 是如何做到类型安全的？

**答案**：它并不做运行期类型检查。无类型版本返回 `void *`（L47），模板版本仅做 `reinterpret_cast<const T *>(...)`（L54-57）。类型正确性靠约定保证：T 必须与算子编译时产出的 CompileInfo 类型一致（对比 TilingParseContext::GetCompiledInfo 的注释「该类型需要与IMPL_OP注册时TilingParse的类型一致」）。用错类型是未定义行为——这是追求零开销的代价。

**练习 2**：为什么 `fe::PlatFormInfos` 在 metadef 中只有前向声明就够了？

**答案**：TilingContext 只透传指针（`GetValue<fe::PlatFormInfos *>()`），从不解引用、不关心其大小和布局。真正的消费在算子仓/fe 侧。这样 metadef 无需依赖 fe 头文件，保持了「基础库不依赖上层」的单向依赖（u1-l1 的结论）。

---

### 4.4 写出 Tiling 结果：TilingOutputIndex 与 TilingData

#### 4.4.1 概念说明

tiling 的产出不是靠返回值，而是写进上下文**输出段**的一组固定槽位。`TilingOutputIndex` 枚举把输出段排布变成了名字：tiling-key、block 维度、atomic 清理标志、tiling data、workspace、tiling condition、schedule mode 等，每个槽位配套一对 `SetXxx` / `GetXxx` 接口。

几个概念解释（面向初学者）：

- **tiling key**：同一个算子可能为不同 shape 档位编译了多个二进制变体，tiling key 是算子告诉框架「本次请选择哪个变体」的编号。
- **block dim / num blocks**：本次执行要开多少个并行块（粗略理解为切给多少个核）。
- **atomic clean flag**：若算子用原子加规约中间结果，框架需要先清零目标缓冲，此标志告诉框架「需要清」。
- **workspace**：算子执行时需要的临时设备内存，tiling 阶段算出大小并向框架申请。
- **TilingData**：自定义切分参数的字节流容器，算子自己定义结构、自己序列化，设备侧 kernel 再按相同布局读出。

#### 4.4.2 核心流程

Set/Get 接口遵循统一模板：

```text
SetXxx(value):
  p = GetOutputPointer<T>(kOutputXxx)   // 取输出槽位指针，越界/未分配返回 nullptr
  if (p == nullptr) return GRAPH_FAILED
  *p = value
  return GRAPH_SUCCESS

GetXxx():
  p = GetOutputPointer<T>(kOutputXxx)
  if (p == nullptr) return 哨兵值        // 数值最大值 / -1 / 0 / false
  return *p
```

`GetTilingData<T>()` 稍特殊：它先拿 raw 容器、校验容量、登记数据长度、再返回可写指针。

#### 4.4.3 源码精读

输出段的排布契约，先看注释再看枚举：

[tiling_context.h:L157-L186](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L157-L186) —— 注释列出 outputs[0]~[8] 的含义，`TilingOutputIndex` 枚举把每个槽位命名：`kOutputTilingKey=0`、`kOutputSimdNumBlocks=1`、`kOutputAtomicCleanFlag=2`、`kOutputTilingData=3`、`kOutputWorkspace=4`、`kOutputTilingCond=5`、`kOutputScheduleMode=6`、`kOutputDynUBufSize=7`、`kOutputAicpuNumBlocks=8`、`kOutputSimtBlockDim=9`、`kOutputSimtGridDim=10`，尾部 `kOutputNum` 是哨兵。注意枚举里大量「旧名与新名同值」的别名（如 `kOutputBlockDim` 与 `kOutputSimdNumBlocks` 都是 1），是接口平滑废弃的手段——枚举值即槽位下标，**只能尾部追加，永不复用**（注释「add new output definitions here」也点明了这一点）。[L191-L195](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L191-L195) 的 `FallibleTilingOutputIndex` 从 `kOutputNum` 起继续编号。

标准 Set 接口的样子：

[tiling_context.h:L202-L209](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L202-L209) —— `SetTilingKey`：取 `GetOutputPointer<uint64_t>(kOutputTilingKey)`，判空、赋值。对应的 `GetTilingKey`（[L214-L220](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L214-L220)）失败返回 `uint64_t` 最大值。

废弃接口的转发写法：

[tiling_context.h:L251-L270](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L251-L270) —— `SetBlockDim` 标记 `@deprecated`，函数体只有一句 `return SetSimdNumBlocks(block_dim);`。新代码应直接用 `SetSimdNumBlocks`。

写 tiling data 的入口：

[tiling_context.h:L394-L412](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L394-L412) —— `GetTilingData<T>()`：取 raw 容器（`GetOutputPointer<TilingData *>(kOutputTilingData)` 解引用一次指针），校验 `GetCapacity() >= sizeof(T)`，`SetDataSize(sizeof(T))` 登记长度，返回 `static_cast<T *>(tiling_data->GetData())`。也就是说：**把你的结构体直接覆写到这块字节流上，长度同时被登记**。

TilingData 容器本身（下一讲 u3-l5 的主角，这里只看骨架）：

[tiling_data.h:L66-L107](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L66-L107) —— `capacity_` / `data_size_` / `data_` 三个字段加 40 字节保留字段，`GetData()` 返回裸字节地址。`Append`/`Expand`（[L109-L150](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_data.h#L109-L150)）支持逐段追加而非整体覆写。

workspace 申请：

[tiling_context.h:L418-L427](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L418-L427) —— `GetWorkspaceSizes(workspace_count)`：输出槽位存放的是 `TypedContinuousVector<size_t>`，先 `SetSize(workspace_count)` 声明个数，返回可写数组首地址，算子把每块 workspace 的字节数填进去。

SIMT 维度（对应 C 接口文档）：

[tiling_context.h:L566-L581](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L566-L581) —— `SetSimtBlockDim(const Dim3 &)` / `GetSimtBlockDim()`：直接以 `Dim3` 结构体读写槽位 9。宿主侧 Builder 的等价 C 接口见 [gert_TilingContextBuilder_SetSimtBlockDim.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/c_api/gert_TilingContextBuilder_SetSimtBlockDim.md)，文档示例展示了 `gert::Dim3 block_dim(2, 3)` 的用法。

#### 4.4.4 代码实践

**实践目标**：读懂 tiling data 定义宏的真实产物，为综合实践做准备。

1. 打开 [tests/ut/register/testcase/tiling_register_unittest.cc:L40-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/tiling_register_unittest.cc#L40-L54)：`BEGIN_TILING_DATA_DEF(TestMaxPoolTilingData)` 定义了一组 `TILING_DATA_FIELD_DEF(类型, 字段名)` 字段，`REGISTER_TILING_DATA_CLASS(TestMaxPool, TestMaxPoolTilingData)` 把结构体与算子名关联。
2. 跟进 `inc/external/register/tilingdata_base.h` 中这些宏的定义，确认它们生成的是一个含字段描述表的标准布局结构体。
3. **观察现象**：宏展开后的结构体字段顺序即序列化后的字节顺序；`GetTilingData<TestMaxPoolTilingData>()` 返回的指针可直接按字段访问。
4. **预期结果**：能说出「算子侧写的 TilingData 结构」与「设备侧 kernel 读的结构」必须由同一组宏生成、逐字段布局一致，否则数据错乱。宏的内部机制在 u3-l5 详述，本实践只需建立直觉。若要实际运行该测试：`bash tests/run_test.sh -u` 后在 `ut_register` 目标中查找 `UtestRegister` 相关用例（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`SetTilingKey` 为什么不设计成构造函数参数或返回值，而要「先拿指针再赋值」？

**答案**：TilingContext 是框架预先分配好的裸内存视图，算子拿到的是指针，构造早已完成；同时 tiling 结果有十几种，逐个返回值传递需要多次调用约定。统一「槽位 + Set/Get」让所有输出共享一套模式，且失败语义统一（nullptr → GRAPH_FAILED），无需异常。

**练习 2**：`GetTilingData<T>()` 中 `tiling_data->SetDataSize(sizeof(T))` 这一行如果被删掉，会发生什么？

**答案**：字节流里虽然写入了 T 的内容，但容器登记的 `data_size_` 仍是旧值（通常为 0）。框架后续按 `GetDataSize()` 拷贝/传递 tiling data 时会截断或丢弃内容，设备侧拿不到参数。这也是整体覆写与 `Append` 追加的区别：前者必须显式登记总长。

**练习 3**：枚举中 `kOutputBlockDim` 和 `kOutputSimdNumBlocks` 同为 1，这种「一名一值」为什么不直接删掉旧名？

**答案**：枚举值是输出槽位下标，属于对外 ABI 契约；已有算子 so 里编译进了旧名字，直接删除会导致编译失败。metadef 用 `@deprecated` 注释 + 转发实现引导新代码用新名，待生态迁移完再删（与 u1-l3 讲的 pkg_inc 渐进迁移是同一策略）。

---

### 4.5 TilingParseContext：编译信息的反序列化上下文

#### 4.5.1 概念说明

4.3 节说 CompileInfo 是「离线编译阶段产生的缓存数据」。它落地成算子包里的一个 json 字符串；执行期算子被加载时，框架会调用算子注册的 **TilingParse 函数**，把 json 反序列化回内存结构，供后续每次 tiling 的 `GetCompileInfo<T>()` 读取。`TilingParseContext` 就是传给 TilingParse 函数的上下文。

它与 TilingContext 的分工：

| | TilingContext | TilingParseContext |
| --- | --- | --- |
| 调用时机 | 每次执行前的 tiling 阶段 | 算子包加载/首次使用时（一次） |
| 输入 | shape、tensor、平台、确定性开关等 | 编译期 json 字符串、平台信息 |
| 输出 | tiling key、block 维度、tiling data 等 | 反序列化后的 CompiledInfo 结构 |
| 接口规模 | 约 60 个公开接口 | 3 个接口 |

#### 4.5.2 核心流程

```text
算子包内: compile_info.json
    │ 框架加载算子 so
    ▼
TilingParse(context):
    json = context->GetCompiledJson()          // 输入槽 0
    platform = context->GetPlatformInfo()      // 输入槽 1
    解析 json → 构造 CompiledInfo 结构
    *context->GetCompiledInfo<T>() = parsed    // 写入输出槽 0
    ▼
之后每次 TilingFunc 内 context->GetCompileInfo<T>() 读到的
就是这块被填好的 CompiledInfo
```

#### 4.5.3 源码精读

整个类只有 53 行，三个接口全部落在固定槽位上：

[tiling_parse_context.h:L21-L29](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_parse_context.h#L21-L29) —— 类同样继承 `ExtendedKernelContext` 且零新增成员；`GetCompiledJson()` 就是 `GetInputValue<const char *>(0)`——输入槽 0 存放 json 字符串指针。

[tiling_parse_context.h:L35-L42](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_parse_context.h#L35-L42) —— `GetCompiledInfo<T>()`：取输出槽 0 的 Chain，`GetValue<T *>()` 拿到框架预分配好的实例指针，算子把解析结果写入该实例。注释强调 T 必须与 `IMPL_OP` 注册时 TilingParse 的类型一致——与 4.3 节练习 1 的约定呼应。

[tiling_parse_context.h:L47-L51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_parse_context.h#L47-L51) —— `GetPlatformInfo()` 取输入槽 1；文件末尾同样有 `static_assert(std::is_standard_layout<...>)` 把 POD 约束固化。

#### 4.5.4 代码实践

**实践目标**：对比两个上下文的「同名接口、不同槽位」。

1. 阅读 `TilingContext::GetPlatformInfo`（tiling_context.h L443-455）：槽位是 `inputs_num + outputs_num + 1`，需要先查 ComputeNodeInfo。
2. 阅读 `TilingParseContext::GetPlatformInfo`（tiling_parse_context.h L47-49）：槽位固定为 1。
3. **观察现象**：两者签名几乎相同，但槽位定位完全不同——因为两个上下文的 values 排布约定不同：TilingContext 的输入段以变长的 shape 序列开头，TilingParseContext 的输入段固定只有 2 个槽（json、platform）。
4. **预期结果**：总结出规律「上下文接口的本质 = 槽位下标公式 + 指针类型解释」，读懂任何一个新上下文（如下一讲的 InferShapeContext）只需先搞清它的槽位排布。纯阅读实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：TilingParse 函数把解析结果写到哪里？为什么不像普通函数那样 `return parsed`？

**答案**：写到输出槽 0 指向的、框架预分配的 `T` 类型实例上（`GetCompiledInfo<T>()` 返回 `T *`）。不返回值的原因：这块实例的生存期由框架管理，要存活到后续多次 tiling 调用；且上下文体系统一「写槽位、返回状态码」的约定，避免跨 so 传 STL 对象破坏 ABI。

**练习 2**：如果 TilingParse 注册时用的类型是 `MyCompileInfo`，而 TilingFunc 里 `GetCompileInfo<OtherInfo>()` 用了别的类型，会发生什么？

**答案**：没有任何运行期检查会拦截——`GetCompileInfo` 只是 `reinterpret_cast`（tiling_context.h L54-57），按错误布局读写内存，属于未定义行为。类型一致性完全靠算子仓自己的代码约定与测试保障。

---

## 5. 综合实践

**任务**：为假想算子 `MyAdd`（2 个输入、1 个输出，属性 `num_cores`）编写完整的 TilingFunc 伪代码，把本讲四个模块的接口全部串起来，并标注每步调用的接口与行号。

```cpp
// 示例代码（非项目原有代码）：MyAdd 的 TilingFunc 伪代码
struct MyAddTilingData {   // 实际工程中用 BEGIN_TILING_DATA_DEF 宏生成
  int32_t perCoreSize;
  int32_t coreNum;
};

ge::graphStatus MyAddTilingFunc(gert::TilingContext *context) {
  // ── 第 1 步：读输入 shape（4.2 节）──
  // TilingContext::GetInputShape，tiling_context.h L63-73
  const gert::StorageShape *x1 = context->GetInputShape(0);
  const gert::StorageShape *x2 = context->GetInputShape(1);
  const gert::Shape &shape = x1->GetStorageShape();      // 运行时 shape（u2-l4）
  const int64_t total = shape.GetShapeSize();

  // ── 第 2 步：读算子属性（4.3 节，接口继承自 ExtendedKernelContext）──
  // ExtendedKernelContext::GetAttrs，extended_kernel_context.h L133-139
  const gert::RuntimeAttrs *attrs = context->GetAttrs();
  // RuntimeAttrs 按索引取属性（属性顺序 = IR 原型定义顺序）
  const int64_t num_cores = attrs->GetAttrPointer<int64_t>(0);

  // ── 第 3 步：读平台信息与编译缓存（4.3 节）──
  // TilingContext::GetPlatformInfo，tiling_context.h L443-455
  (void)context->GetPlatformInfo();
  // TilingContext::GetCompileInfo<T>，tiling_context.h L54-57（本例无缓存，跳过）

  // ── 第 4 步：计算切分方案并写出结果（4.4 节）──
  // GetTilingData<T>，tiling_context.h L394-405
  MyAddTilingData *td = context->GetTilingData<MyAddTilingData>();
  if (td == nullptr) { return ge::GRAPH_FAILED; }        // 容量不足等失败
  td->coreNum = static_cast<int32_t>(num_cores);
  td->perCoreSize = static_cast<int32_t>((total + num_cores - 1) / num_cores);

  // SetSimdNumBlocks，tiling_context.h L263-270
  if (context->SetSimdNumBlocks(static_cast<uint32_t>(num_cores)) != ge::GRAPH_SUCCESS) {
    return ge::GRAPH_FAILED;
  }
  // SetTilingKey，tiling_context.h L202-209（本算子只有一个变体，key=0）
  (void)context->SetTilingKey(0);
  // GetWorkspaceSizes，tiling_context.h L418-427（需要一块临时缓冲）
  size_t *ws = context->GetWorkspaceSizes(1);
  if (ws == nullptr) { return ge::GRAPH_FAILED; }
  ws[0] = static_cast<size_t>(total) * sizeof(float);
  return ge::GRAPH_SUCCESS;
}
```

**验证方式**：

1. **静态核对**：逐行对照本讲引用的行号，确认每个接口的失败语义（哪些返回 nullptr、哪些返回 GRAPH_FAILED、哪些返回哨兵值）。
2. **动态验证（可选，需要本地环境）**：参考 u5-l1 将讲解的 `OpTilingContextBuilder`，用 Builder 依次 `AddInput`/`AddOutput`/`AddAttr` 构造上下文后调用 `MyAddTilingFunc`，断言 tiling data 与 block dim 与预期一致；或仿照 `tests/ut/register/testcase/tiling_register_unittest.cc` 的 `BEGIN_TILING_DATA_DEF` 宏定义 `MyAddTilingData` 后，用 `bash tests/run_test.sh -u` 跑通。**待本地验证**。

## 6. 本讲小结

- TilingContext 是框架传给算子 TilingFunc 的唯一参数，整条继承链（KernelRunContext → KernelContext → ExtendedKernelContext → TilingContext）零新增数据成员，末尾 `static_assert(is_standard_layout)` 把 POD 布局固化为 ABI 契约。
- 所有接口的本质是「槽位下标公式 + 指针类型解释」：输入 shape/tensor 占 values 前 inputs_num 个槽（同一槽位可按 `StorageShape`/`Tensor`/`TensorV2` 多视角解释），输出 shape 紧随其后，compile_info/platform/tiling_func/deterministic 以 `inputs+outputs+N` 的算式追加在输入段尾部。
- 按 IR 序号访问（Required/Optional/DynamicInput 系列）经 `AnchorInstanceInfo` 翻译成物理槽位，REQUIRED/OPTIONAL 是 DYNAMIC 取第 0 个实例的语法糖。
- tiling 结果统一写入 `TilingOutputIndex` 枚举定义的输出槽位（tiling-key、num-blocks、tiling-data、workspace……），Set/Get 成对出现，失败返回 GRAPH_FAILED 或哨兵值；枚举值即下标，只能尾部追加。
- `GetTilingData<T>()` 把自定义结构体覆写到容量受控的字节流并登记长度，类型正确性完全靠算子侧约定，无运行期检查。
- TilingParseContext 是加载期的一次性上下文：从固定槽位读编译期 json 与平台信息，把反序列化结果写进框架预分配的 CompiledInfo 实例，供之后每次 tiling 的 `GetCompileInfo<T>()` 读取。

## 7. 下一步学习建议

- **下一讲（u3-l4）**：推理上下文三兄弟 `InferShapeContext` / `InferDataTypeContext` / `InferShapeRangeContext`。学完本讲你已经掌握「先看槽位排布、再看接口」的方法论，届时会发现它们是同一套骨架在图编译期的变体。
- **u3-l5（TilingData 专题）**：深入 `inc/external/register/tilingdata_base.h` 的宏体系与 `base/runtime/tiling_data.cc`，弄清 4.4 节实践中一笔带过的字段描述表与序列化细节。
- **u5-l1（ContextBuilder）**：想知道框架侧「第 4.1.2 节流程图的前 3 步」具体怎么填槽位，`base/context_builder` 目录会给出完整答案，也是编写 TilingFunc 单测的关键工具。
- **延伸阅读**：`docs/zh/api/c_api/` 目录下有大量 `gert_TilingContextBuilder_*` 与 `gert_TilingContext_*` 开头的 C 接口文档，可作为接口速查表。
