# u3-l2 ExtendedKernelContext 与 KernelRunContext：上下文的扩展层

## 1. 本讲目标

上一讲（u3-l1）我们读到了执行上下文的最底层骨架：纯 C 结构体 `KernelRunContext`、类型擦除值槽 `Chain`，以及在其上提供随机访问的 `KernelContext`。本讲沿着继承链向上走一层，学完后你应该能够：

1. 说出 `KernelRunContext` 每个字段的含义和 48 字节内存布局契约，理解「头部 + 柔性指针数组」的设计。
2. 解释 `ExtendedKernelContext` 为什么用 `protected` 继承 `KernelContext`、为什么不新增任何数据成员，以及它如何把 `compute_node_info` / `kernel_extend_info` 两个 `void *` 指针转换成类型化的 `ComputeNodeInfo` / `KernelExtendInfo`。
3. 描述框架（context builder）如何在分配好的裸内存上构造 `KernelRunContext`，并理解 `reinterpret_cast` 在这套体系里出现的两个位置。
4. 定位 tiling / infer 等具体上下文（`TilingContext`、`InferShapeContext`……）从 `ExtendedKernelContext` 继承到的公共能力清单。

## 2. 前置知识

- **POD 与 standard_layout**：C++ 中布局简单、可以用 `memcpy`/`memset` 安全操作、跨编译单元布局一致的对象。判断标准是 `std::is_standard_layout<T>::value`。上一讲已说明：上下文对象要跨 so 边界传递、要在裸内存上直接构造，所以整条继承链都被 `static_assert(std::is_standard_layout<...>)` 锁死。
- **protected 继承**：`class B : protected A` 表示 A 的 public 成员在 B 中变成 protected，外界无法把 `B*` 当作 `A*` 使用。这是一种「实现复用但不对外暴露基类接口」的手段——后面会看到 metadef 用它把 `KernelContext` 的底层槽位访问收起来，只留下类型化的高层接口。
- **`void *` + `static_cast` 的类型化**：C 结构体里只能存 `void *`，C++ 侧拿到后用 `static_cast<const T *>(ptr)` 恢复类型。这是 C 布局与 C++ 接口共存的最常见桥接方式。
- **`reinterpret_cast` 指针重解释**：把一块内存「当作」某种类型的对象使用，不做任何运行期检查。它成立的前提是对象布局严格一致——这正是整条继承链零新增成员、全 POD 的原因。
- **柔性数组思想**：C99 之前的惯用法，结构体最后一个成员是 `T values[1]`，实际分配 `sizeof(结构体) + n * sizeof(T)`，让 `values` 「越界」延伸成 n 个元素的数组。`KernelRunContext` 用的就是这个技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/external/exe_graph/runtime/kernel_run_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h) | 纯 C 头文件，定义 `AsyncAnyValue` 与 `KernelRunContext` 两个底层结构体，是整个上下文体系的内存布局契约 |
| [inc/external/exe_graph/runtime/kernel_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h) | C++ 侧第一个视图：`Chain` 与 `KernelContext`（上一讲主角，本讲只引用其中两个 extend 指针接口） |
| [inc/external/exe_graph/runtime/extended_kernel_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h) | 本讲主角：`ExtendedKernelContext`，在 `KernelContext` 之上提供计算节点信息 / kernel 扩展信息的类型化访问 |
| [inc/external/exe_graph/runtime/context_extend.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/context_extend.h) | `KernelExtendInfo` 的定义：`kernel_extend_info` 指针真正指向的类型，带 56 字节保留字段 |
| base/context_builder/op_context_builder_impl.cc / .h | 框架侧构造代码：`BuildCtx` 演示如何在裸内存上搭出 `KernelRunContext`（本讲第 4 个模块精读，详细 Builder 体系留到 u5-l1） |
| tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc | ABI 守护测试：断言各上下文类 sizeof 均为 48、字段偏移不变 |
| tests/ut/base/testcase/context_builder_unittest.cc | 上下文构建的单元测试，展示 `reinterpret_cast<KernelContext *>` 的合法使用场景 |

## 4. 核心概念与源码讲解

### 4.1 KernelRunContext：纯 C 的内存布局契约

#### 4.1.1 概念说明

`KernelRunContext` 是执行上下文的「物理层」：一个纯 C 结构体，描述一个 kernel 被调用时框架传给它的全部裸数据——输入输出槽数量、两个扩展信息指针、以及值槽数组。头文件里两句注释说得很直白：「Chain 的底层数据结构，不要直接引用和操作此数据结构」「KernelContext 的底层数据结构，不要直接引用和操作此数据结构」——它是给 C++ 视图类铺底座的，业务代码不应直接碰。

之所以用纯 C 结构体而不是 C++ 类，是为了 ABI 安全：C 结构体的布局由字段顺序和编译目标唯一决定，不随编译器版本、标准库版本、编译选项（如 `_GLIBCXX_USE_CXX11_ABI`）变化。这样 ge 框架 so 分配的上下文内存，可以安全地被另一个编译环境的算子 so 按 `KernelContext` 视图读取。

#### 4.1.2 核心流程

64 位平台下 `KernelRunContext` 的内存布局（总 48 字节，ABI 测试锁定的数值）：

```text
偏移   字段                  大小    含义
0      input_size            8       输入值槽数量
8      output_size           8       输出值槽数量
16     compute_node_info     8       指向 ComputeNodeInfo 块（节点名/类型/IR原型信息/属性）
24     kernel_extend_info    8       指向 KernelExtendInfo 块（kernel 名/类型）
32     output_start          8       指向 values[input_size]，输出槽的起点（源码标注 todo delete this）
40     values[1]             8       柔性指针数组占位：每个元素是 AsyncAnyValue*（即 Chain*）
```

关键约定：

1. **实际分配大小**是 `sizeof(KernelRunContext) + sizeof(Chain *) * (input_size + output_size)`，`values[1]` 只是占位，真正的槽指针延伸到结构体之后。
2. **槽序列扁平排列**：输入在前、输出在后，所以「输出 i」的物理位置是 `values[input_size + i]`；`output_start` 就是这个位置的首地址缓存。
3. **values 存的是指针的指针**：`values[i]` 指向一个独立的 `AsyncAnyValue`（也就是 C++ 侧的 `Chain`）对象，Chain 再决定数据是内联在 8 字节槽里还是挂在堆指针上（上一讲已精读）。
4. 两个 extend 指针都是 `const void *`，类型化推迟到 C++ 层完成——这正是 4.2 模块的主角。

#### 4.1.3 源码精读

`AsyncAnyValue`：16 字节的数据槽，union 存指针或内联字节，配一个删除器回调：

[inc/external/exe_graph/runtime/kernel_run_context.h:L20-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L20-L31)

这段代码定义了 `FreeCallback` 函数指针类型和 `AsyncAnyValue` 结构体（union 数据槽 + deleter）。它整体被包在 `extern "C"` 块里（L16-L18），保证 C 链接约定、名字不修饰。

`KernelRunContext` 本体：

[inc/external/exe_graph/runtime/kernel_run_context.h:L36-L43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L36-L43)

这段代码就是 4.1.2 表格的来源：两个 size 计数、两个 extend 指针、`output_start`（注释 `// todo delete this` 说明它是待清理的冗余缓存）和 `values[1]` 柔性占位。

布局契约由 ABI 测试锁定：

[tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:L252-L266](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L252-L266)

这段测试断言 `sizeof(KernelRunContext) == 48`，并逐字段检查偏移（`input_size` 在偏移 0、各字段依次紧贴）。任何人给结构体中间插一个字段，这个测试立刻失败——这就是「布局即契约」的工程化守护。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 `KernelRunContext` 的布局推导，而不是背表格。
2. **操作步骤**：写一个独立的小程序（示例代码，非项目原有代码）：

   ```cpp
   // 示例代码：verify_layout.cpp
   #include <cstdio>
   #include <cstddef>
   #include "exe_graph/runtime/kernel_run_context.h"

   int main() {
     printf("sizeof(AsyncAnyValue)      = %zu\n", sizeof(gert::AsyncAnyValue));
     printf("sizeof(KernelRunContext)   = %zu\n", sizeof(gert::KernelRunContext));
     printf("offsetof(output_start)     = %zu\n", offsetof(gert::KernelRunContext, output_start));
     printf("offsetof(values)           = %zu\n", offsetof(gert::KernelRunContext, values));
     return 0;
   }
   ```

   编译时把 metadef 的 `inc/external` 加入头文件搜索路径（参考 u1-l2 讲过的构建环境）。
3. **需要观察的现象**：输出应为 16、48、32、40（64 位平台）。
4. **预期结果**：与 [abi_compatibility_for_exe_graph_unittest.cc:L46](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L46) 中 `kKernelRunContextSize = 48U` 一致。若不一致，说明你的平台指针宽度不是 8 字节（如 32 位环境）。
5. 本实践结果**待本地验证**（依赖可用的编译环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `values` 声明为 `AsyncAnyValue *values[1]`（指针数组），而不是 `AsyncAnyValue values[1]`（对象数组）？

**答案**：因为 `AsyncAnyValue`（即 Chain）承载的数据由框架在宿主侧逐个填充，很多槽指向调用方保有生命周期的对象（如 `StorageShape`），用指针数组可以让框架把 Chain 对象集中放在另一块连续内存（builder 的 `value_holder_`），上下文里只存指针；同时保持 `KernelRunContext` 头部大小与槽数解耦——实际分配大小按 `sizeof(Chain *) * 槽数` 扩展，而不是按 `sizeof(Chain) * 槽数`。

**练习 2**：如果要在 `KernelRunContext` 里新增一个字段，正确的做法是什么？

**答案**：只能加在 `values` 之前的头部**末尾附近**并同步更新 ABI 测试的偏移断言——实际上更稳妥的做法是不改这个结构体：两个 extend 指针（`compute_node_info`、`kernel_extend_info`）就是为此预留的扩展通道，新增信息应放进它们指向的扩展块（`KernelExtendInfo` 甚至预留了 56 字节）。改动头部字段顺序或在中部插入字段会直接破坏 ABI，被 4.1.3 引用的测试拦截。

---

### 4.2 ExtendedKernelContext：protected 继承的类型化扩展层

#### 4.2.1 概念说明

先做一个重要的**勘误**：学习大纲和实践任务里提到「列出 ExtendedKernelContext 提供的全部 with* 方法」——对照当前 HEAD 的源码，`ExtendedKernelContext` **并没有任何 `with*` 方法**（在 `inc/external/exe_graph` 全目录检索 `With`，命中的只有 `SetWithDefaultDeleter`、`IsSharedWith` 等别的类的接口）。大纲里的「with 语义」应理解为**「在 KernelContext 之上做类型化包装」的扩展语义**：它不是靠 with 前缀方法，而是靠 `protected` 继承 + `void*` 到具体类型的指针转换实现的。本节按真实源码给出完整方法清单。

`ExtendedKernelContext` 解决的问题是：`KernelContext` 只提供「第 i 个输入槽」这种**位置语义**的访问（`GetInput(i)`），而算子代码需要的是**业务语义**——「我的 IR 原型第 0 个输入的描述」「这个节点的属性」「这个 kernel 的名字」。这些信息藏在 `compute_node_info` / `kernel_extend_info` 两个 `void *` 指针背后，`ExtendedKernelContext` 的全部工作就是把它们取出来、转成正确类型、并做越界判空。

两个设计要点：

1. **`protected` 继承**：外界不能把 `ExtendedKernelContext*` 转成 `KernelContext*` 去直接戳槽位，底层接口被收进 protected 区，只对派生类（`TilingContext` 等）开放。
2. **零新增数据成员**：整个类只有成员函数，sizeof 仍等于基类的 48 字节。这保证了同一块 `KernelRunContext` 内存可以被任何层级的视图类安全地重解释。

#### 4.2.2 核心流程

一条典型的取值链路（以 `GetDynamicInputDesc(ir_index, relative_index)` 为例）：

```text
ExtendedKernelContext::GetDynamicInputDesc(ir, rel)
  └─> GetComputeNodeInfo()
        └─> KernelContext::GetComputeNodeExtend()      // 取 context_.compute_node_info（void*）
        └─> static_cast<const ComputeNodeInfo *>(...)  // 类型化
  └─> GetIrInputInstanceInfo(ir)                        // 查 IR 原型第 ir 个输入的实例化信息
        └─> compute_node_info->GetInputInstanceInfo(ir) // 返回 AnchorInstanceInfo*
  └─> 越界检查：ins_info->GetInstanceNum() <= rel ? 返回 nullptr
  └─> compute_node_info->GetInputTdInfo(ins_info->GetInstanceStart() + rel)
        // 实例化起点 + 相对索引 = 物理槽位，取出编译期 Tensor 描述
```

注意「IR 索引」与「物理索引」的映射：一个 `DYNAMIC_INPUT` 在 IR 原型里占一个位置，实例化后可能展开成多个物理输入。`AnchorInstanceInfo` 记录了「实例化数量」和「起始物理编号」，把 IR 索引翻译成物理槽位——这是 `REQUIRED_INPUT`/`OPTIONAL_INPUT`/`DYNAMIC_INPUT` 三类输入统一寻址的基础。

#### 4.2.3 源码精读

类的声明与继承方式：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L18)

这一行是本讲的题眼：`class ExtendedKernelContext : protected KernelContext`——protected 继承收起底层接口，且类体内没有任何数据成员声明（直到 L224 的 `};`）。

三个输入描述接口，注意前两个的实现完全相同：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L37-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L37-L47)

`GetOptionalInputDesc` 与 `GetRequiredInputDesc` 都委托给 `GetDynamicInputDesc(ir_index, 0)`——对单个实例化的输入而言，「取第 0 个实例」与「取必选输入」是同一件事，两个命名只是给调用方表达 IR 原型意图的语法糖。

动态输入的完整寻址（4.2.2 流程图的落点）：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L55-L68](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L55-L68)

这段代码完成「IR 索引 + 相对实例索引 → 物理 TensorDesc」的三步转换，任何一步失败（节点信息缺失、IR 索引非法、relative_index 超出实例化数量）都返回 nullptr，不抛异常——延续整个 runtime 体系的空值失败语义。

两个扩展信息的类型化出口：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L166-L168](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L166-L168)

[inc/external/exe_graph/runtime/extended_kernel_context.h:L195-L197](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L195-L197)

`GetComputeNodeInfo` 和 `GetExtendInfo` 是全类仅有的两处指针类型化：分别把基类取出的 `const void *`（见 [kernel_context.h:L257-L266](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L257-L266) 的 `GetComputeNodeExtend` / `GetKernelExtend`）转成 `const ComputeNodeInfo *` / `const KernelExtendInfo *`。类里其余十几个 Get 方法全部是围绕这两个出口的二次封装。

protected 区里给派生类（如 `TilingContext`）准备的动态槽位模板：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L212-L223](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L212-L223)

`GetDynamicOutputPointer` 是「扁平槽序列」约定的直接消费者：输出槽在物理上排在 `input_num` 之后，所以它用 `GetInputPointer<T>(Offset + input_num + 起始 + 相对索引)` 取输出——名字叫 InputPointer，取的却是输出，原因是基类只提供了基于 values 数组前段的寻址函数，输出复用了同一条寻址路径。模板参数 `Offset` 允许派生上下文在真实输入之前再塞几个框架槽（如 TilingContext 的 compile_info、platform_info，见 4.4 模块）。

末尾的布局锁：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L225-L226](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L225-L226)

`static_assert(std::is_standard_layout<ExtendedKernelContext>::value, ...)` 把「零新增成员、布局与基类一致」从设计意图固化为编译期约束（断言消息里的 "ExtendedKernelRunContext" 是历史名称笔误，不影响行为）。ABI 测试进一步断言其 sizeof 仍为 48：[abi_compatibility_for_exe_graph_unittest.cc:L275-L280](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L275-L280)。

**当前 HEAD 下 `ExtendedKernelContext` 的完整方法清单**（勘误后的「with* 方法」答案）：

| 类别 | 方法 | 可见性 |
| --- | --- | --- |
| 输入描述 | `GetInputDesc`、`GetOptionalInputDesc`、`GetRequiredInputDesc`、`GetDynamicInputDesc` | public |
| 输出描述 | `GetOutputDesc` | public |
| 实例化信息 | `GetIrInputInstanceInfo`、`GetIrOutputInstanceInfo` | public |
| 计算节点 | `GetComputeNodeInputNum`、`GetComputeNodeOutputNum`、`GetAttrs`、`GetNodeType`、`GetNodeName`、`GetComputeNodeInfo` | public |
| kernel 扩展 | `GetKernelName`、`GetKernelType`、`GetExtendInfo` | public |
| 动态槽位模板 | `GetDynamicInputPointer<T, Offset>`、`GetDynamicOutputPointer<T, Offset>` | protected |

下游派生类（均可在 `inc/external/exe_graph/runtime/` 下找到）：

| 派生上下文 | 声明位置 |
| --- | --- |
| `TilingContext` | [tiling_context.h:L34](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L34) |
| `InferShapeContext` | [infer_shape_context.h:L30](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L30) |
| `InferDataTypeContext` / `InferShapeRangeContext` | infer_datatype_context.h:L23 / infer_shape_range_context.h:L23 |
| `TilingParseContext` | [tiling_parse_context.h:L21](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_parse_context.h#L21) |
| `OpExecuteContext` / `OpExecuteLaunchContext` / `OpExecutePrepareContext` / `ExeResGenerationContext` / `OpCheckContext` | op_execute_context.h:L48 等 |

也就是说，算子侧在任何阶段回调里能拿到的「节点是谁、IR 原型长什么样、属性是什么」，全部由本类提供——这就是学习目标里「定位 tiling/infer 等具体上下文的公共基类能力」的答案。

#### 4.2.4 代码实践

1. **实践目标**：完成大纲要求的方法盘点，并验证「零新增成员」的布局不变性。
2. **操作步骤**：
   - 通读 [extended_kernel_context.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h) 全文，按 4.2.3 的表格逐个核对方法与可见性，特别确认检索不到任何 `with*` 方法（可用 `grep -n "With" inc/external/exe_graph/runtime/extended_kernel_context.h`，结果为空）。
   - 写一个最小程序（示例代码，非项目原有代码）验证三个类的 sizeof 相等：

   ```cpp
   // 示例代码：sizeof_chain.cpp
   #include <cstdio>
   #include "exe_graph/runtime/kernel_context.h"
   #include "exe_graph/runtime/extended_kernel_context.h"
   #include "exe_graph/runtime/tiling_context.h"

   int main() {
     printf("sizeof(KernelContext)         = %zu\n", sizeof(gert::KernelContext));
     printf("sizeof(ExtendedKernelContext) = %zu\n", sizeof(gert::ExtendedKernelContext));
     printf("sizeof(TilingContext)         = %zu\n", sizeof(gert::TilingContext));
     return 0;
   }
   ```

3. **需要观察的现象**：三个输出应完全相同（64 位平台均为 48）。
4. **预期结果**：与 [abi_compatibility_for_exe_graph_unittest.cc:L268-L315](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L268-L315) 对 `KernelContext`、`ExtendedKernelContext`、`InferShapeContext`、`TilingContext`、`TilingParseContext` 等逐个断言 `sizeof == 48` 一致。若你在 `TilingContext` 里看到了成员变量导致输出不同，请检查是否误读了其他命名空间的同名类。
5. 本实践结果**待本地验证**（依赖可用的编译环境）；也可以直接运行该 gtest 用例替代，见 4.4.4。

#### 4.2.5 小练习与答案

**练习 1**：`ExtendedKernelContext` 为什么继承 `KernelContext` 用 `protected` 而不是 `public`？如果改成 `public` 会有什么后果？

**答案**：public 继承意味着外界（算子代码）可以把 `TilingContext*` 向上转型成 `KernelContext*`，直接用 `GetInput(i)`/`MutableInput(i)` 按**物理槽位**戳数据，绕过 IR 索引校验和 Offset 约定——tiling 上下文的 values 前段其实混着 compile_info、platform_info 等框架槽（见 4.4 模块），按裸槽位访问会取到错误数据。protected 继承把这些底层接口收进派生类内部，强制外部走类型化的业务语义接口。

**练习 2**：`GetRequiredInputDesc` 和 `GetOptionalInputDesc` 实现一模一样，为什么要留两个名字？

**答案**：给调用方表达意图。IR 原型里 `REQUIRED_INPUT` 与 `OPTIONAL_INPUT` 是两种声明（决定算子必须/可选提供该输入），读取「该 IR 位置实例化后的第 0 个输入」逻辑相同，但调用 `GetOptionalInputDesc` 的代码读者能立刻知道「这里处理的是可选输入，返回 nullptr 是预期内的正常分支」，可读性和 API 自文档性更好。

**练习 3**：`GetDynamicOutputPointer` 里为什么要加 `input_num`？

**答案**：因为 `KernelRunContext` 的 values 槽序列是扁平的「输入在前、输出在后」，输出 j 的物理位置是 `values[input_size + j]`；`GetDynamicOutputPointer` 计算的是物理槽位下标，所以要在 IR 输出实例化起点之外再叠加 `GetComputeNodeInputNum()`（必要时还有 `Offset`），与 4.1.2 的布局约定一一对应。

---

### 4.3 KernelExtendInfo 与 ComputeNodeInfo：两个扩展信息块

#### 4.3.1 概念说明

`KernelRunContext` 头部那两个 `const void *` 指针分别指向两块独立分配的扩展信息：

- **`ComputeNodeInfo`**（定义在 `compute_node_info.h`，被 context_extend.h 引入）：描述「这个 kernel 对应的计算节点」——节点名/类型、IR 输入输出数量与实例化信息（`AnchorInstanceInfo` 数组）、每个物理输入输出的编译期描述（`CompileTimeTensorDesc`）、以及节点的属性（`RuntimeAttrs`）。它是变长结构，按 `CalcSize(ir_input_num, ir_output_num, input_num, output_num, attr_size)` 计算总大小一次分配（u5-l1 会详细讲）。
- **`KernelExtendInfo`**（本模块精读）：描述「这个 kernel 本身」——kernel 名与 kernel 类型，外加一块 56 字节的保留字段。

设计动机是**开放封闭**：上下文头部结构体不能随便改（ABI），新增信息一律塞进扩展块；`KernelExtendInfo` 甚至预留了 56 字节，就是为了未来加字段时不改头部、不改已有字段布局。

#### 4.3.2 核心流程

`KernelExtendInfo` 的内存布局（8 + 8 + 56 = 72 字节）：

```text
偏移   字段            大小    含义
0      kernel_name_    8       指向字符串常量池的指针（不持有内存）
8      kernel_type_    8       指向字符串常量池的指针
16     reserved_[56]   56      保留字段，当前禁止直接使用
```

访问路径：`ExtendedKernelContext::GetKernelName()` → `GetExtendInfo()`（static_cast）→ `GetKernelExtend()`（取 `context_.kernel_extend_info`）→ `KernelExtendInfo::GetKernelName()`（返回 `kernel_name_`）。任何一环为空即返回 nullptr。

#### 4.3.3 源码精读

类的全貌：

[inc/external/exe_graph/runtime/context_extend.h:L18-L60](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/context_extend.h#L18-L60)

这段代码有三个值得注意的点：

1. **构造函数全部 `= delete`**（L50-L54）：不允许在栈/堆上正常构造这个对象。它是被框架在预先分配好的裸内存上直接按指针摆放的（`SetKernelName` 只是赋值指针，不调用构造函数），禁掉构造函数防止使用者绕过框架自行创建，破坏字符串指针的生命周期管理。
2. **两个字符串成员只存指针不持有内存**（L57-L58）：字符串本体由框架侧的 `string_pool`（builder 里的 `std::vector<std::string>`）持有，`KernelExtendInfo` 只留地址——POD 结构体里放 `std::string` 会破坏 standard_layout，这与 u2-l2 讲过的 AscendString 动机一致。
3. **保留字段注释**（L59）：`uint8_t reserved_[56];  // Reserved field, 32+8, do not directly use when only 8-byte left`——56 字节按「32 + 8 + …」的节奏预留，注释提醒未来加字段时至少留 8 字节余量。`SetKernelType` 里的 `(void)reserved_;`（L46）是为了压住编译器对未使用字段的告警。

布局守护：

[inc/external/exe_graph/runtime/context_extend.h:L61](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/context_extend.h#L61)

static_assert 锁定 standard_layout；单测进一步检查字段偏移：[abi_compatibility_for_exe_graph_unittest.cc:L240-L249](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L240-L249)（用 `malloc` 裸内存 + `reinterpret_cast` 构造后逐字段断言偏移，这个测试本身也是 4.4 模块「裸内存构造」的官方示范）。

消费方在 `ExtendedKernelContext` 中：

[inc/external/exe_graph/runtime/extended_kernel_context.h:L173-L190](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L173-L190)

`GetKernelName` / `GetKernelType` 就是「取 extend 指针 → 判空 → 读字段」的三行封装，是本模块与 4.2 模块的连接点。

#### 4.3.4 代码实践

1. **实践目标**：理解「extend 指针可以为空」的失败语义，并掌握 `KernelExtendInfo` 的尺寸。
2. **操作步骤**：
   - 写一个小程序（示例代码，非项目原有代码），在一块 72 字节的裸内存上手工摆一个 `KernelExtendInfo`，再通过 `ExtendedKernelContext` 的只读路径之外的方式直接读它：

   ```cpp
   // 示例代码：extend_info.cpp
   #include <cstdio>
   #include <cstring>
   #include "exe_graph/runtime/context_extend.h"

   int main() {
     printf("sizeof(KernelExtendInfo) = %zu\n", sizeof(gert::KernelExtendInfo));
     // 模拟框架：在裸内存上摆放 KernelExtendInfo（真实框架见 4.4 的 BuildCtx）
     alignas(8) unsigned char buf[sizeof(gert::KernelExtendInfo)] = {0};
     auto *kei = reinterpret_cast<gert::KernelExtendInfo *>(buf);
     kei->SetKernelName("add_kernel");
     kei->SetKernelType("aicore");
     printf("name=%s type=%s\n", kei->GetKernelName(), kei->GetKernelType());
     return 0;
   }
   ```

3. **需要观察的现象**：sizeof 输出 72；打印出 `name=add_kernel type=aicore`。
4. **预期结果**：与 ABI 测试对 `KernelExtendInfo` 偏移的断言（reserved_ 在尾部）一致。注意示例里直接调 `SetKernelName` 是因为构造函数被 delete，只能走指针赋值——这正是框架的做法。
5. 本实践结果**待本地验证**。另外思考一个不需要编译的观察题：`compute_node_info` 为 nullptr 时 `GetKernelName()` 一定能返回 nullptr 吗？答案见下面练习 2。

#### 4.3.5 小练习与答案

**练习 1**：`KernelExtendInfo` 为什么把构造函数全部 delete，而不提供一个 `Init(const char *, const char *)`？

**答案**：它必须保持 standard_layout 且由框架在裸内存上按指针摆放（构造函数语义会引入 vtable/初始化逻辑的不确定性）。delete 构造函数是对使用者的硬约束：对象只能由框架在受控内存上创建，字符串指针的生命周期由框架的 string_pool 统一管理，避免局部构造后指针悬垂。

**练习 2**：`GetKernelName()` 判空的是 `GetExtendInfo()`（即 `kernel_extend_info` 指针），而 `GetNodeType()` 判空的是 `GetComputeNodeInfo()`。两者判空对象为什么不同？

**答案**：因为两个方法读取的是**不同的扩展块**：kernel 名/类型放在 `KernelExtendInfo`（`kernel_extend_info` 指针），节点名/类型放在 `ComputeNodeInfo`（`compute_node_info` 指针）。两块信息由不同代码在不同时机填充（框架侧 vs 节点信息侧），任何一块都可能缺席，所以各自独立判空。这也回答了 4.3.4 的观察题：`compute_node_info` 为空不影响 `GetKernelName()`。

---

### 4.4 框架如何在裸内存上构造 KernelRunContext

#### 4.4.1 概念说明

前三模块都在「读」上下文，本模块讲「写」：一块 `new uint8_t[size]` 的裸内存，如何变成一个合法的 `KernelRunContext`，再被 `reinterpret_cast` 成 `TilingContext` 交给算子的 tiling 函数。这套构造代码在 `base/context_builder/`（Builder 体系的完整讲解是 u5-l1 的任务，这里只看与内存布局直接相关的核心函数）。

关键在于理解：**上下文体系里没有任何虚函数、没有任何构造调用**。「构造」= 分配足够大的字节数组 + 按布局逐字段填值；「类型转换」= `reinterpret_cast` 换个视图。这一切成立的唯一保障就是 4.1-4.3 锁死的布局契约。

#### 4.4.2 核心流程

`ContextBuilderImpl::BuildCtx` 的完整流程：

```text
输入: input_values_(输入 (指针, deleter) 列表)、output_values_(输出列表)
1. size = sizeof(KernelRunContext) + sizeof(Chain *) * (输入数 + 输出数)
2. context_holder_ = make_unique<uint8_t[]>(size)      // 裸内存
3. context_ = PtrToPtr<uint8_t, KernelContext>(裸内存)  // 第一次视图转换
4. 填头部: input_size / output_size / compute_node_info / output_start
5. value_holder_ 里为每个槽创建 Chain 对象，values[i] 指向第 i 个 Chain
6. 逐槽调用 Chain::Set(数据指针, deleter)
产出: ContextHolderImpl 持有全部内存，GetContext<T>() 用 reinterpret_cast<T*> 提供类型化出口
```

内存从属关系全景：

```text
ContextHolderImpl (唯一所有权)
 ├─ context_holder_: uint8_t[]     ──> 被视为 KernelRunContext/KernelContext/TilingContext（同一块内存）
 ├─ value_holder_: vector<Chain>   ──> 被 KernelRunContext::values[i] 逐个指向
 ├─ compute_node_info_holder_      ──> 被 compute_node_info 指针指向
 └─ string_pool_: vector<string>   ──> 被 ComputeNodeInfo/KernelExtendInfo 里的 char* 指向
```

#### 4.4.3 源码精读

构造主函数：

[base/context_builder/op_context_builder_impl.cc:L116-L142](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L116-L142)

这段代码是 4.4.2 流程图的逐行对应，几个细节值得圈出来：

- L120：分配大小公式 `sizeof(KernelRunContext) + sizeof(Chain *) * io_size`，正是 4.1.2 说的柔性数组扩展；注意 `values` 元素是指针（8 字节）而非 Chain 对象（16 字节）。
- L124：`PtrToPtr<uint8_t, KernelContext>` 把裸字节指针转成 `KernelContext *`——之后**没有任何构造函数被调用**，字段靠 L126-L128 直接赋值。
- L129：`output_start = &values[input_size]`，与 4.1.2 的布局约定互相印证。
- L132：`values[i]` 指向 `value_holder_` 里的第 i 个 Chain（`PtrToPtr<Chain, AsyncAnyValue>` 佐证了「Chain 与 AsyncAnyValue 是同一内存的两种语言视图」）。
- L134-L140：输入输出依次 `Set` 进各自 Chain——输入占据前段槽位，输出紧随其后。

持有者与类型化出口：

[base/context_builder/op_context_builder_impl.h:L65-L68](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.h#L65-L68)

这段代码是 `reinterpret_cast<T *>(context_)` 模板：同一块内存按调用方指定的类型（`TilingContext`、`InferShapeContext`……）取出，这是 `reinterpret_cast` 在本体系的第一个关键位置。同文件 L38-L75 的 `ContextHolderImpl` 则是 4.4.2 全景图里那个「唯一所有权」的落地：三个 holder 加字符串池，析构时对每个 Chain `Set(nullptr, nullptr)` 触发各槽 deleter（L60-L64）。

Tiling 上下文如何在这块内存上「多出」专属槽位：

[base/context_builder/op_tiling_context_builder.cc:L23-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L23-L47)

注意 `TilingContext` 没有任何自有成员变量，它的「专属数据」（TilingData、workspace、compile_info、platform_info……）全部是**追加到 values 槽序列尾部的额外槽位**（L30-L38 把输出和 tiling 专属信息依次 emplace 进 input_values_），由 `TilingContext` 的读取接口配合 `Offset` 常量（4.2.3 提到的 `GetDynamicInputPointer<T, Offset>`）按位置解释。这就是「零新增成员却各有专属数据」的完整答案。

`reinterpret_cast` 的第二个关键位置在测试里（也是官方认可的用法示范）：

[tests/ut/base/testcase/context_builder_unittest.cc:L35-L64](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L35-L64)

L47 `reinterpret_cast<KernelContext *>(holder.GetContext())`：Builder 产出的是不透明持有者，测试（以及框架调度代码）用 reinterpret_cast 拿回类型化视图，随后 `GetComputeNodeExtend()` / `GetInput(i)->GetPointer<StorageShape>()` 的读取结果与写入一一对应。整段测试就是「构造 → 转换 → 读回验证」的闭环样板。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：不看 Builder 自己动手，在一块裸内存上构造最小 `KernelRunContext`，并验证经 `KernelContext` 视图能读回写入的数据——彻底理解「构造 = 填字段，转换 = 换视图」。
2. **操作步骤**：
   - 编写如下程序（示例代码，非项目原有代码，仅依赖 metadef 对外头文件）：

   ```cpp
   // 示例代码：raw_ctx.cpp —— 在裸内存上构造 KernelRunContext
   #include <cstdio>
   #include <memory>
   #include <vector>
   #include "exe_graph/runtime/kernel_context.h"

   int main() {
     using namespace gert;
     // 步骤1：准备 2 输入 + 1 输出的数据（生命周期必须覆盖读取全程）
     int64_t a = 100, b = 200, c = 0;
     const size_t inNum = 2U, outNum = 1U;
     const size_t size = sizeof(KernelRunContext) + sizeof(Chain *) * (inNum + outNum);
     std::unique_ptr<uint8_t[]> raw(new uint8_t[size]{});
     std::vector<Chain> chains(inNum + outNum);  // 槽对象，模拟 value_holder_

     // 步骤2：按 KernelContext 视图填头部与 values（不调用任何构造函数）
     auto *ctx = reinterpret_cast<KernelContext *>(raw.get());
     auto *krc = ctx->GetContext();
     krc->input_size = inNum;
     krc->output_size = outNum;
     krc->compute_node_info = nullptr;         // 本例不建 ComputeNodeInfo
     krc->kernel_extend_info = nullptr;
     krc->output_start = &(krc->values[inNum]);
     for (size_t i = 0U; i < inNum + outNum; ++i) {
       krc->values[i] = reinterpret_cast<AsyncAnyValue *>(&chains[i]);
     }
     chains[0].Set(&a, nullptr);               // 输入0：a
     chains[1].Set(&b, nullptr);               // 输入1：b
     chains[2].Set(&c, nullptr);               // 输出0：c

     // 步骤3：经视图读回，验证构造正确
     std::printf("GetInputNum()=%zu GetOutputNum()=%zu\n", ctx->GetInputNum(), ctx->GetOutputNum());
     std::printf("in0=%lld in1=%lld out0=%lld\n",                // int64_t 用 %lld 打印
                 (long long)*ctx->GetInputPointer<int64_t>(0U),
                 (long long)*ctx->GetInputPointer<int64_t>(1U),
                 (long long)*ctx->GetOutputPointer<int64_t>(0U));
     return 0;
   }
   ```

   - 编译：`g++ -std=c++11 -I<metadef仓库>/inc/external raw_ctx.cpp -o raw_ctx`（本例不链接 `libmetadef.so`，因为用到的全是头文件 inline 代码；若链接报缺失符号，说明你的编译环境头文件版本不同）。
   - 对照真实框架代码检查你的实现与 [op_context_builder_impl.cc:L116-L142](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L116-L142) 的差异。
3. **需要观察的现象**：输出 `GetInputNum()=2 GetOutputNum()=1`、`in0=100 in1=200 out0=0`；把 `chains[0].Set(&a, nullptr)` 改成别的值再运行，读回结果同步变化，证明读写的确是同一块内存。
4. **预期结果**：如上。若输出乱码或崩溃，优先检查：a/b/c 是否在读取前离开作用域（生命周期的铁律）；`values[i]` 是否忘了填（nullptr 会让 `GetInput` 返回 nullptr，`GetInputPointer` 返回 nullptr 后解引用崩溃）。
5. 本实践结果**待本地验证**。无法编译时，替代方案是阅读并运行现成测试：`bash tests/run_test.sh -u` 后执行 `./build_gcov/ut_metadef --gtest_filter='UtestContextBuilder.CreateKernelRunContextOK'`（gtest 目标名与运行方式见 u1-l2），该用例即 4.4.3 引用的官方版本。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BuildCtx` 分配的是 `sizeof(Chain *) * io_size` 而不是 `sizeof(Chain) * io_size`？如果把 Chain 对象直接内联在 `KernelRunContext` 之后会怎样？

**答案**：因为 `KernelRunContext::values` 的类型是 `AsyncAnyValue *values[1]`（指针数组），布局契约规定每个槽 8 字节存指针，Chain 本体放哪里是自由的——builder 选择放进独立的 `value_holder_` 向量，方便统一析构（对每个 Chain 调 `Set(nullptr, nullptr)` 释放 deleter）。若强行内联在结构体之后，布局公式和 ABI 测试都要改，而且 Chain 的生命周期就与上下文内存绑死，无法分开管理。

**练习 2**：框架里出现过哪两类「把内存当成另一种类型」的操作？各出现在哪里？

**答案**：一类是 `static_cast<const ComputeNodeInfo *>(void *)` / `static_cast<const KernelExtendInfo *>(void *)`（`extended_kernel_context.h` L166-L168、L195-L197），把 C 结构体里的 void 指针恢复成具体类型；另一类是 `reinterpret_cast<T *>(raw)`（`op_context_builder_impl.h` L67 的 `GetContext<T>()`、测试里的 `reinterpret_cast<KernelContext *>(holder.GetContext())`），把整块裸内存按某个上下文类视图化。前者是「指针指向什么」的类型化，后者是「这块内存是什么」的视图切换；两者都安全的前提是整条继承链 zero-member、standard_layout、sizeof 全等于 48。

**练习 3**：`TilingContext` 没有数据成员，它的 TilingData 输出、compile_info 输入放在哪里？`InferShapeContext` 与它共用这块内存布局吗？

**答案**：都作为额外槽位追加在 values 扁平序列的尾部（见 `op_tiling_context_builder.cc` L30-L44：输出、TilingCompileInfo、PlatformInfo、Deterministic 等依次 emplace），由 `TilingContext` 读取接口按固定偏移解释。共用——`InferShapeContext` 与 `TilingContext` sizeof 同为 48（ABI 测试 L282-L315 分别断言），差异只在各自接口对槽位序列的「解释协议」（Offset 常量与 GetXxx 的槽位编号约定）不同。

## 5. 综合实践

**任务：亲手复刻一条「裸内存 → KernelRunContext → 视图读取」迷你链路，并量化布局契约。**

结合本讲全部四个模块，完成一个 `mini_context.cpp`（示例代码）：

1. **布局量化**（模块 4.1）：打印 `sizeof(AsyncAnyValue)`、`sizeof(KernelRunContext)`、`KernelRunContext` 各字段偏移，确认 16/48/0/8/16/24/32/40。
2. **裸内存构造**（模块 4.4）：按 4.4.4 的步骤分配 `sizeof(KernelRunContext) + sizeof(Chain *) * 3` 的内存，填好头部、output_start 与 values，2 输入 1 输出。
3. **extend 指针留空的失败语义验证**（模块 4.2/4.3）：保持 `compute_node_info = nullptr`，尝试构造一个 `ExtendedKernelContext` 视图（示例代码）：

   ```cpp
   auto *ext = reinterpret_cast<gert::ExtendedKernelContext *>(raw.get());
   std::printf("GetComputeNodeInputNum()=%zu\n", ext->GetComputeNodeInputNum());  // 预期 0
   std::printf("GetAttrs()=%p\n", static_cast<const void *>(ext->GetAttrs()));    // 预期 nil
   ```

   注意：示例直接对裸内存 reinterpret_cast 是合法的，但真实代码中 `ExtendedKernelContext` 的构造入口应交给 Builder（其 protected 继承也阻止了你随便向上转型）。
4. **槽位读写验证**：经 `KernelContext` 视图读回输入 0/1 与输出 0，确认与写入一致；再故意把读取下标改为 2 去取「输入」，观察越界保护（`GetInput(2)` 返回 nullptr，因为 2 >= input_size）。
5. **对照收尾**：把你的构造代码与 [op_context_builder_impl.cc:L116-L142](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L116-L142) 逐行对比，列出你省略了什么（答案应包含：ComputeNodeInfo 块的创建、string_pool、deleter 传递）。

预期全部输出与各步骤标注一致；编译运行依赖本地环境，**待本地验证**。完成后你就独立走通了一遍框架构造上下文的核心路径，为 u5-l1（完整 Builder 体系）和 u3-l3（TilingContext 详解）打好了底子。

## 6. 本讲小结

- `KernelRunContext`（[kernel_run_context.h:L36-L43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L36-L43)）是纯 C 的 48 字节布局契约：头部计数 + 两个 extend 指针 + output_start + `values[1]` 柔性指针数组，实际分配大小按槽数扩展，槽序列「输入在前、输出在后」扁平排列。
- `ExtendedKernelContext` 用 **protected 继承 + 零新增成员**在 `KernelContext` 之上提供类型化业务接口；**勘误**：它没有任何 `with*` 方法，真实 API 是十几个 `Get*` 方法（4.2.3 有完整清单），核心是两处 `static_cast`（`GetComputeNodeInfo`/`GetExtendInfo`）。
- IR 索引到物理槽位的翻译由 `AnchorInstanceInfo`（实例化数量 + 起始编号）完成，`REQUIRED/OPTIONAL` 输入读取是 `DYNAMIC` 读取取第 0 个实例的语法糖。
- 两个扩展块各司其职：`ComputeNodeInfo` 描述计算节点（IR 原型、TensorDesc、属性），`KernelExtendInfo`（72 字节，含 56 字节保留字段）描述 kernel 本身；新增信息走扩展块，不动头部。
- 框架构造上下文 = 「裸内存 + 逐字段赋值」，类型转换 = `reinterpret_cast` 换视图；`TilingContext` 等派生类的专属数据是追加在 values 序列尾部的槽位，而非成员变量。
- 整套机制的安全性由三层保障兜底：类内 `static_assert(is_standard_layout)`、ABI 测试的 sizeof/偏移断言、接口层全线「返回空值不抛异常」。

## 7. 下一步学习建议

- **下一讲 u3-l3（TilingContext）**：带着本讲的「槽位偏移协议」视角去读 `tiling_context.h`——你会看到 `kOutputTilingData`、compile_info、platform_info 等槽位常量如何与 `GetDynamicInputPointer<T, Offset>` 配合，把扁平槽序列解释成 tiling 阶段的丰富接口。
- **u3-l4（推理上下文）**：对比 `InferShapeContext` / `InferDataTypeContext` / `InferShapeRangeContext` 三套「解释协议」的差异，验证本讲「同一块内存、不同视图」的结论。
- **提前浏览 `compute_node_info.h`**：`ExtendedKernelContext` 一半的方法是对它的转发，读懂它的变长布局（CalcSize/Init）能让 u5-l1 的 Builder 学习事半功倍。
- **重温 ABI 测试**：[abi_compatibility_for_exe_graph_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc) 是本讲所有布局结论的机器可验证版本，u5-l4 将从架构层面正式讨论 ABI 兼容设计。
