# u5-l4 ABI 兼容性设计与守护测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 metadef 为什么对 ABI（Application Binary Interface，应用二进制接口）兼容性有近乎苛刻的要求。
2. 理解 POD / `std::is_standard_layout` 约束在 ABI 中的作用，以及 `static_assert` 如何把布局契约固化为编译期错误。
3. 掌握 `abi_compatibility_for_exe_graph_unittest.cc` 的写法：它守护哪些结构体、用什么手段守护、守护到什么粒度。
4. 在二次开发中，用一份检查清单判断「一处改动是否破坏 ABI」，并知道哪些演进手法（reserved 字段、尾部追加）是安全的。

## 2. 前置知识

### 2.1 什么是 ABI，为什么源码兼容不等于二进制兼容

很多读者熟悉 API（源码层面接口）：只要函数名、参数没变，调用方源码不用改就能重新编译通过。但 CANN 是一整套**预编译好的二进制组件**（`.so` 共享库）的组合：ge、各算子仓、runtime 都带着已经编译好的 `.so` 依赖 metadef 导出的 `libmetadef.so`。如果 metadef 改了一个结构体的内存布局，而某个依赖方没有重新编译，那么：

- metadef 侧按新布局写入第 N 个字节的数据，依赖方按旧布局去读第 N 个字节——读到的是错位的垃圾数据；
- 或者 metadef 认为结构体是 48 字节、调用方认为它是 56 字节，栈上/堆上的内存越界访问。

这种「不用重新编译对方、二进制直接互操作」的兼容性就是 **ABI 兼容**。API 兼容是「重新编译后没问题」，ABI 兼容是「不重新编译也没问题」。README 在贡献约束里明确写了这一点：

- [README.md:47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L47)：「metadef 的接口变更需要保持 ABI 兼容，随意修改可能导致其他组件无法正常工作」。
- [README.md:60](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L60)：提交流程的自检清单里包含「保持 ABI 兼容性」。

### 2.2 POD 与 standard_layout

- **POD**（Plain Old Data）：可以按 C 结构体方式安全 memcpy、跨语言边界传递的类型。
- **`std::is_standard_layout<T>`**：C++ 类型萃取，判断 T 是否为「标准布局」——所有成员拥有相同的访问控制、没有虚函数/虚基类、成员排布可预测。标准布局是跨编译单元、跨 so 传递结构体的最低要求。

一句话直觉：**只要一个类型的 `sizeof`、每个成员的偏移量、对齐方式都不变，它的 ABI 就没变。** 反过来，下面任何一条都会破坏 ABI：

| 改动 | 破坏原因 |
| --- | --- |
| 增加虚函数 | 对象头部被插入 vptr（8 字节），所有成员偏移量整体后移，且不再是 standard_layout |
| 在中间插入新成员 | 后面所有成员偏移量改变 |
| 在末尾追加成员 | `sizeof` 变大，按旧大小分配内存的一方越界 |
| 调整成员顺序 | 偏移量错乱 |
| 把成员换成 `std::string`/`std::vector` 等 STL 类型 | STL 类型内部布局随标准库版本、编译选项（如 `_GLIBCXX_USE_CXX11_ABI`）变化 |

前四条都是「布局变了」；最后一条是 u2-l2 讲过的结论——这也是对外头文件里只出现 `AscendString`、`const char_t *` 而不出现 `std::string` 的原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `inc/external/exe_graph/runtime/kernel_run_context.h` | 纯 C 的底层结构：`AsyncAnyValue`、`KernelRunContext`，是最底层的 ABI 契约 |
| `inc/external/exe_graph/runtime/kernel_context.h` | `Chain` 与 `KernelContext`，C++ 视图层，末尾 `static_assert` 固化布局 |
| `inc/external/exe_graph/runtime/extended_kernel_context.h` | `ExtendedKernelContext`，protected 继承 + 零新增成员的类型化视图，同样有 `static_assert` |
| `tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc` | 本讲主角：用硬编码常量断言 20 余个结构体的 sizeof 与逐字段偏移量 |
| `README.md` | 项目对 ABI 兼容的约束声明 |

## 4. 核心概念与源码讲解

### 4.1 POD 约束：metadef 是如何「设计出」ABI 稳定结构的

#### 4.1.1 概念说明

exe_graph 运行时体系（gert 命名空间）的设计哲学在 u3-l1、u3-l2 已经建立：所有上下文类（`KernelContext`、`TilingContext`、`InferShapeContext`……）**零新增数据成员**，只是同一块 `KernelRunContext` 内存的不同类型化视图。本模块从 ABI 视角重新审视这个设计——为什么偏偏要这样设计？因为只有这样才能让「框架分配的内存」和「算子 so 读取的内存」在双方不同版本、不同编译时间的前提下仍然对齐。

配套的守护手段是散布在各头文件末尾的 27 处 `static_assert(std::is_standard_layout<T>::value, ...)`（用 Grep 在 `inc/external/exe_graph/runtime/` 下可数出），它们把「必须是标准布局」从文档约定升级为**编译期错误**。

#### 4.1.2 核心流程

一个 gert 结构体从定义到被守护的流程：

```text
kernel_run_context.h 定义纯 C 结构体（AsyncAnyValue / KernelRunContext）
        │
        ▼
kernel_context.h 等头文件定义 C++ 视图类（Chain / KernelContext / TilingContext ...）
   ├─ 类内零虚函数、零 STL 成员，只包含 POD 成员或指向 POD 的指针
   └─ 末尾 static_assert(is_standard_layout<T>) —— 编译期第一道防线
        │
        ▼
abi_compatibility_for_exe_graph_unittest.cc
   ├─ ASSERT_EQ(sizeof(T), 硬编码字节数)          —— 编译期后、运行期的第二道防线
   └─ 逐成员 reinterpret_cast 取地址，断言偏移量    —— 第三道防线（最细粒度）
```

三道防线的分工：

1. **static_assert**：拦住「让类型不再是 standard_layout」的改动（加虚函数、加访问控制混乱的成员）。改动者在编译阶段就报错，成本最低。
2. **sizeof 断言**：拦住「仍是 standard_layout 但总大小变了」的改动（末尾追加成员、把 `int32` 换 `int64`）。
3. **偏移量断言**：拦住「总大小没变但内部错位」的改动（成员顺序调整、前面字段变宽的同时后面 reserved 缩小）。

#### 4.1.3 源码精读

最底层是两个纯 C 结构体——注意它们包在 `extern "C"` 里，连 C++ name mangling 都不参与，这是最原始也最稳定的 ABI 形态：

[inc/external/exe_graph/runtime/kernel_run_context.h:25-L43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L25-L43)：`AsyncAnyValue` 是 16 字节的「数据槽 + deleter 回调」，`KernelRunContext` 是「头部计数 + 两个扩展指针 + `values[1]` 柔性数组」。注释直接写明「不要直接引用和操作此数据结构」——它们是给 C++ 视图类吃的原料。

往上一层，`Chain` 是 C++ 视图，全部成员只有一个 `AsyncAnyValue any_value_`，所有方法都是对这个 POD 的位操作：

[inc/external/exe_graph/runtime/kernel_context.h:16-L54](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L16-L54)：`Chain` 的 `GetPointer<T>` 按编译期 `sizeof(T)` 分流——小于等于 8 字节内联存于 `data.inplace`，大于 8 字节存 `data.pointer`。这正是 u3-l1 讲过的类型擦除值槽。

[inc/external/exe_graph/runtime/kernel_context.h:127-L129](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L127-L129)：`Chain` 唯一成员之后紧跟 `static_assert(std::is_standard_layout<Chain>::value, "The class Chain must be a POD")`。任何人给 `Chain` 加虚函数，所有包含此头文件的编译单元立刻编译失败。

[inc/external/exe_graph/runtime/kernel_context.h:131-L321](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L131-L321)：`KernelContext` 全部方法都是读 `context_` 的 inline 函数，唯一数据成员是 [第 319 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L319) 的 `KernelRunContext context_`，第 321 行同样以 `static_assert` 收尾。

[inc/external/exe_graph/runtime/extended_kernel_context.h:18-L18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/extended_kernel_context.h#L18-L18)：`ExtendedKernelContext : protected KernelContext`——注意是 **protected 继承**且**不声明任何数据成员**，第 225 行的 `static_assert` 再次确认它和 `KernelRunContext` 一样是标准布局。protected 继承在这里的作用是「复用布局但收窄接口」，避免派生视图绕过基类直接裸摸底层结构。

同一模式在整个 runtime 目录复用——`TilingContext` 在 [tiling_context.h:840](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L840)、`InferShapeContext` 在 [infer_shape_context.h:119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/infer_shape_context.h#L119)、`Tensor` 在 [runtime_tensor.h:343](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L343)，全部以同一条断言收尾。

#### 4.1.4 代码实践

**实践目标**：亲手数一遍 static_assert 防线，并验证「加虚函数 = 编译失败」。

**操作步骤**：

1. 在仓库根目录执行 Grep（或 `grep -rn`）：

   ```bash
   grep -rn "static_assert(std::is_standard_layout" inc/external/exe_graph/runtime/
   ```

   记录命中的文件数与总条数（本讲编写时为 27 条，若与你本地不符，以本地为准）。

2. 在本地一次性试验分支上（**不要合入**），给 `Shape`（`inc/external/exe_graph/runtime/shape.h`）临时加一个虚函数：

   ```cpp
   // 示例代码：仅用于本地观察编译行为，观察完立即还原
   class Shape {
    public:
     virtual ~Shape() {}   // 假想改动：引入虚函数
     ...
   ```

3. 重新编译（任选一个包含该头文件的目标即可，例如 `bash build.sh` 或 `bash tests/run_test.sh -u`）。

**需要观察的现象**：编译在 `shape.h` 的 `static_assert(std::is_standard_layout<Shape>::value, ...)` 处直接报错，错误信息就是断言里的字符串 "The class Shape must be a POD"。你甚至来不及进入单元测试阶段——第一道防线已经拦截。

**预期结果**：编译失败于 static_assert；还原改动后编译恢复。若你在本地复现，记录下具体错误输出（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KernelContext` 的方法全部写成头文件内的 inline 函数，而不是声明在头文件、实现在 .cc？

**参考答案**：inline 函数在调用方编译单元内展开，不产生对 `libmetadef.so` 符号的运行期绑定；即便未来这些函数的实现被修改（逻辑变了但签名没变），调用方不需要重新链接。同时这符合 u1-l3 讲过的「inline 函数与声明无体函数」的区分：对外薄视图类的取值逻辑全部 inline，真正编入 so 的只有少数复杂实现（如 `GetSizeInBytes` 走 `TypeImpl`）。

**练习 2**：给 `Chain` 增加一个 `std::string name_` 成员，会同时踩中几条防线？

**参考答案**：两条。其一，`std::string` 不是 standard_layout 语义下的安全跨 ABI 类型（且 `Chain` 将不再满足 `is_standard_layout`，第 129 行 static_assert 编译期报错——不同标准库实现下 `std::string` 布局不同）；其二，即便某种实现下侥幸通过，`sizeof(Chain)` 也会从 16 字节增大，ABI 测试中 [abi_compatibility_for_exe_graph_unittest.cc:221-L227](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L221-L227) 的 `ASSERT_EQ(sizeof(c), kChainSize)` 也会失败。

### 4.2 ABI 守护测试精读：sizeof 与逐字段偏移断言

#### 4.2.1 概念说明

`static_assert` 只能拦住「不再是 standard_layout」这一类改动；对「仍是 standard_layout 但布局变了」的改动（末尾加成员、字段顺序调整）它无能为力。`tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc` 就是补上这个缺口的第二、三道防线：**把当前布局的每一个数字（总大小、每个成员的偏移量、保留字段的大小）用硬编码常量固化下来**。任何人改动布局，哪怕只偏 1 个字节，测试立刻红。

这个测试的思想可以概括为一句话：**布局本身也是一种对外契约，契约就要有对应的测试。**

#### 4.2.2 核心流程

测试对每个结构体 `T` 的检查套路固定为三步：

```text
1. ASSERT_EQ(sizeof(T), 硬编码总大小)                      —— 总量不变
2. ASSERT_EQ(&T 第一个成员地址, &T 地址)                    —— 首成员无前置填充
3. 对每个相邻成员 (m_i, m_{i+1})：
     EXPECT_EQ(成员 m_{i+1} 的地址 - 成员 m_i 的地址, 期望字节数) —— 逐段偏移不变
   末尾通常再断言 reserved_ 的大小 —— 预留空间不变
```

取成员地址的手法是 `reinterpret_cast<uintptr_t>(&obj.member_) - reinterpret_cast<uintptr_t>(&obj)`，即用指针算术测量编译器实际排布出来的偏移。所有期望值集中在文件开头的匿名命名空间常量里（[第 24-50 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L24-L50)），一眼可审计。

#### 4.2.3 源码精读

先看常量表——整份契约的「数字面」：

[tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:24-L50](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L24-L50)：`kShapeSize = 248`、`kTensorSize = 752`、`kKernelRunContextSize = 48`、`kTilingDataSize = 64`……以及反复出现的 `kReservedFieldSize = 40`。每个常量都有对应的测试消费。

看一个最典型的用例：

[tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:54-L64](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L54-L64)：`Shape_CheckMemLayoutNotChanged` 断言 `sizeof(Shape) == 248`、`dim_num_` 位于偏移 0、`dims_` 距它 8 字节、`reserved_` 距 `dims_` 恰好 `25 * sizeof(int64_t)`（即 kMaxDimNum=25 个维度）、`reserved_` 占 40 字节。等一下——8 + 200 + 40 = 248，数字全部对上，这份测试本身就是一份精确到字节的 `Shape` 布局说明书。

两个值得玩味的细节：

- [tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:151-L171](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L151-L171)：对 `ComputeNodeInfo`、`RuntimeAttrs`（[173-181 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L173-L181)）、`KernelExtendInfo`（[239-250 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L239-L250)）、`TilingData`（[317-330 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L317-L330)），测试不走默认构造，而是 `malloc` 一块指定大小的内存再 `reinterpret_cast` 成对象指针。原因是这些类构造函数不公开（只能由框架在裸内存上落盘，呼应 u5-l1 的 Builder），同时也顺带验证了「这些类型可以安全地在 malloc 内存上按位解释」这一 ABI 场景本身。

- [tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:183-L202](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L183-L202)：`RuntimeAttrsDef` 的 `offset` 成员 `sizeof(...) == 0`、`ContinuousVector` 的 `elements` 成员 `sizeof(...) == 8`——这是 C/C++ 的柔性数组（`type elements[]` / `values[1]`）成员，测试用「sizeof 为 0 或 1 个元素大小」把它们也纳入契约。

上下文类继承链的守护则体现「零新增成员」的验收方式：

[tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:268-L315](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L268-L315)：`KernelContext`、`ExtendedKernelContext`、`InferShapeContext`、`InferShapeRangeContext`、`InferDataTypeContext`、`TilingContext`、`TilingParseContext` 七个类全部断言 `sizeof == 48 == kKernelRunContextSize`、首成员地址等于对象地址。谁要是给某个上下文类「顺手」加了一个数据成员，sizeof 立刻超过 48，七个用例中对应的那条变红。这正是 u3-l2 讲过的「派生类只是同一块内存的类型化视图」的测试化表达。

被守护结构体清单（按测试用例出现顺序）：`Shape`、`StorageShape`、`ExpandDimsType`、`StorageFormat`、`TensorData`、`Tensor`、`CompileTimeTensorDesc`、`AnchorInstanceInfo`、`ComputeNodeInfo`、`RuntimeAttrs`、`RuntimeAttrsDef`、`ContinuousVector`（含 `TypedContinuousVector` 模板实例）、`ContinuousVectorVector`、`Chain`、`Range<Shape>`、`KernelExtendInfo`、`KernelRunContext`、`KernelContext`、`ExtendedKernelContext`、`InferShapeContext`、`InferShapeRangeContext`、`InferDataTypeContext`、`TilingContext`、`TilingParseContext`、`TilingData`——共 25 个用例（其中两个是模板/变体形态）。

还有一行容易被忽略的代码：

[tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:51-L51](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L51-L51)：`constexpr size_t Shape::kMaxDimNum;` 在类外重复声明这个 `static constexpr` 成员，是 C++17 前风格的 ODR-use 写法，让测试能取其地址并断言值为 25（[第 63 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L63)）——连「最大维度数」这个语义常量也在守护范围内。

#### 4.2.4 代码实践

**实践目标**：跑通 ABI 测试，并亲手制造一次「布局破坏」观察测试如何拦截（或为何拦截不到）。

**操作步骤**：

1. 按 u1-l2 讲过的方式运行单元测试并只筛 ABI 用例：

   ```bash
   bash tests/run_test.sh -u
   # 或在构建目录中精确执行：
   ctest -R AbiCompatibilityForExeGraphUT -L ut
   ```

   记录 25 个用例全部 PASS。

2. 在本地试验分支上，给 `Shape` 的 `reserved_` 之前插入一个新成员 `int64_t magic_;`（这是「中间插入」型破坏，static_assert 拦不住，因为仍是 standard_layout）。

3. 重新运行同一测试。

**需要观察的现象**：`Shape_CheckMemLayoutNotChanged` 失败。失败点会精确指出是哪一条断言——`sizeof(s)` 从 248 变成 256，同时 `reserved_` 的偏移量断言也失败。

4. 再换一种「假想」破坏：给 `Shape` 加 `virtual ~Shape();`。

**需要观察的现象**：这次连编译都过不去——`shape.h` 末尾的 static_assert 报 "The class Shape must be a POD"。也就是说虚函数这条路被第一道防线拦截，根本轮不到单测。

**预期结果**：第一种改动被单测拦截（sizeof 与偏移断言红），第二种改动被编译期 static_assert 拦截。两条路径都被堵死，这就是「分层守护」的意义。以上现象待本地验证。

**重要提醒**：试验完成后务必还原源码——本实践只允许在本地丢弃分支上进行，禁止把破坏性改动带入任何提交。

#### 4.2.5 小练习与答案

**练习 1**：为什么总大小断言用 `ASSERT_EQ`，而成员偏移断言大多用 `EXPECT_EQ`？

**参考答案**：`ASSERT_*` 失败后立即终止当前用例，后续语句不再执行。总大小或首成员偏移一旦错了，后面的偏移测量没有意义（甚至可能对未按预期布局的对象取地址），应当立刻止损；而中间某一段偏移错了，后面的段仍值得继续报告，方便一次看清全部错位点，所以用 `EXPECT_*` 继续跑完。这是 gtest 的通用惯例在本测试里的正确应用。

**练习 2**：测试为什么把期望值全部写成文件开头的 `constexpr` 常量，而不是直接在断言里写数字？

**参考答案**：其一，可审计——审阅者在一处就能看到全部「布局契约数字」；其二，常量之间可以互相推导（例如 `kTensorSize = kStorageShapeSize + kStorageFormatSize + ...` 这类关系在断言里直接复用常量名表达），布局的组成逻辑一目了然；其三，避免魔法数字散落导致的抄写错误，改一处常量即可联动多个断言。

### 4.3 守护范围与安全的演进手法：reserved 字段、尾部追加与改动判断清单

#### 4.3.1 概念说明

知道「什么不能改」只是半个知识点，另半个是「怎么改才能不破坏 ABI」。metadef 源码里反复出现的 `reserved_` 字段就是为此准备的：**在结构体尾部预埋一段不用的填充空间，未来需要新增字段时，从 reserved 空间里「切」一块出来用，总大小和既有成员偏移量都不变**。这就是为什么 `kReservedFieldSize = 40` 会在十几个结构体里反复出现。

同时要清醒认识守护的**边界**：这份测试只覆盖 exe_graph（gert）体系的结构体；ge 老体系（`ge::Tensor`、`GeTensorDesc` 等）没有同类偏移测试，但它们用另一种手法保 ABI——pimpl（u2-l4 讲过 `ge::Tensor` 是 `shared_ptr<Impl>` 壳，对外类只有一个指针成员，真实字段藏在 .cc 里的 `Impl` 中，怎么改都不动对外布局）以及 `AnyValue` 的 16 字节类型无关布局（u2-l3）。也就是说 metadef 的 ABI 策略是「两条腿」：gert 体系靠 POD + 布局测试硬守护，ge 体系靠 pimpl/类型擦除软隔离。

#### 4.3.2 核心流程

判断一处改动是否破坏 ABI 的决策流程：

```text
改动涉及对外头文件（inc/external、pkg_inc）里的结构体/类？
├─ 否 → 只影响源码兼容（API），重新编译即可，ABI 风险低
└─ 是 ↓
   改动是否触碰数据成员？
   ├─ 只改 inline 成员函数体、新增非虚成员函数 → ABI 安全（不改变布局；新增符号是追加）
   ├─ 在 reserved_ 空间内切出新字段，总大小与既有偏移不变 → ABI 安全（需同步更新 ABI 测试常量）
   ├─ 枚举/常量尾部追加 → ABI 安全（既有取值不变，参考 u2-l1 的 DataType/Format）
   ├─ 新增/修改虚函数、插入/删除/调序数据成员、改变成员类型宽度
   │    → 破坏 ABI：static_assert（若破坏 standard_layout）或 ABI 单测（若破坏 sizeof/偏移）将拦截
   └─ 拿不准 → 对照 README 的贡献约束，按「跨仓公共需求 + 可保持 ABI 兼容」标准评估
```

#### 4.3.3 源码精读

reserved 字段的实例——`TensorData` 的布局中，真实字段只占前 32 字节，尾部却是连排的两个保留段：

[tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:98-L111](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L98-L111)：`TensorData` 总 72 字节 = `addr_`(8) + `manager_`(8) + `size_`(8) + `placement_`(8) + `reserved_0_`(4) + `reserved_1_`(40 中的 36...) ——测试逐段钉死了每个字段的位置，其中 `reserved_1_` 独占 40 字节。未来若 `TensorData` 需要新字段（比如记录 dehydration 信息），正确做法是从 `reserved_1_` 里取空间，而不是在末尾追加。

同样地，`ComputeNodeInfo` 尾部有 24 字节保留段（`kComputeNodeInfoReservedFieldSize = 24`，[第 26 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L26)）、`KernelExtendInfo` 尾部有 56 字节保留段（`kExtendInfoReservedFieldSize = 56`，[第 27 行](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L27)，对应 u3-l2 讲过的 72 字节含 56 字节保留）。保留段大小是「弹药库存」：切一次少一块，用尽之前必须规划新版本机制。

另一个演进手法是 u3-l3、u4-l4 中反复出现的「枚举/结构只能尾部追加」：`TilingOutputIndex` 枚举值即输出槽位下标、`OpImplFunctionsV2` 末尾的 `st_size/version/reserved` 三重守护，本质与本讲的 reserved 字段同源——**新东西永远加在尾部，老取值永不复用**。

头文件目录边界（u1-l3 建立的知识）在 ABI 语境下的含义也要补一句：`inc/external/` 下的头文件是 ABI 契约本体，`pkg_inc/` 是随 CANN 包发布的快照。一个改动如果只动 `base/` 下的 `.cc` 实现而不动 `inc/external/` 布局，ABI 不受影响；一旦动了 `inc/external/` 中结构体的成员排布，就必须过本讲的测试这一关。

#### 4.3.4 代码实践

**实践目标**：完成规格中要求的核心实践——列出守护清单，并推演「给结构体加虚函数」的两种结局。

**操作步骤**：

1. 打开 `tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc`，把 25 个 `TEST_F(AbiCompatibilityForExeGraphUT, Xxx_CheckMemLayoutNotChanged)` 用例逐个抄录成一张表（本讲 4.2.3 已给出参考答案，请自行核对一遍），按「值类型（直接构造）/ 裸内存型（malloc + reinterpret_cast）」分两列。

2. 选定 `KernelRunContext` 作为「假想」受害者，推演两条破坏路径：

   - **路径 A（加虚函数）**：给 [kernel_run_context.h:36-L43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_run_context.h#L36-L43) 的 C 结构体思路在 C++ 视图类上加虚函数。以 `KernelContext` 为例：加一个 `virtual ~KernelContext()` 会在对象头插入 8 字节 vptr，`sizeof` 从 48 变 56，且 `is_standard_layout` 不再成立。结局：[kernel_context.h:321](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L321) 的 static_assert **编译期直接报错**，测试根本没机会跑——被第一道防线拦截。
   - **路径 B（不加虚函数，末尾加普通成员）**：比如给 `KernelContext` 加 `int flag_;`。仍是 standard_layout，static_assert 放行；但 `sizeof` 变为 56（对齐填充），[abi_compatibility_for_exe_graph_unittest.cc:268-L273](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L268-L273) 的 `ASSERT_EQ(sizeof(KernelContext), kKernelRunContextSize)` 在**运行期变红**——被第二道防线拦截。

3. 回答关键的「绕过」问题：**有没有既加虚函数又不被拦的方法？** 有——不去改结构体，而是新造一个类（例如 `class KernelContextV2`）另立门户，老结构原封不动。这正是 metadef 现实中的演进方式：`Tensor` 之外新增 `TensorV2`（u2-l4）、注册体系之外新增 `OpImplRegisterV2`（u4-l4）、`GetOutput` 之外新增 `GetOutput2`（[kernel_context.h:191-L196](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/kernel_context.h#L191-L196)）。**ABI 演进的正解是并列新版本，而不是修改旧版本。**

**需要观察的现象**：清单中「裸内存型」用例（ComputeNodeInfo、RuntimeAttrs、KernelExtendInfo、TilingData）均以 malloc 构造；「值类型」用例直接栈上构造。

**预期结果**：两条破坏路径分别被 static_assert 与单测拦截；唯一安全的「结构级演进」是新增并列类型。若想实际复现路径 A/B，参照 4.1.4/4.2.4 的本地试验步骤（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`TilingContext` 需要暴露一个新的平台信息指针，下列三种方案哪些破坏 ABI？为什么？
A. 在类里加成员 `void *extra_;`
B. 利用 `KernelExtendInfo` 的 56 字节保留段存这个指针
C. 通过 values 槽位序列尾部追加一个新槽位传递

**参考答案**：A 破坏——`TilingContext` 的 sizeof 必须等于 48（[abi_compatibility_for_exe_graph_unittest.cc:303-L308](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L303-L308)），加成员即变红，且会影响所有按 48 字节分配内存的调用方。B 安全——总大小与既有偏移不变，只需同步更新守护 `KernelExtendInfo` 的常量与断言（把保留段切小）。C 安全且是既有惯例——u3-l3 讲过隐藏槽位以「inputs+outputs+N」公式追加，新槽位加在尾部不影响旧算子（老代码按旧公式取槽，读不到新槽位但也不会错位）。

**练习 2**：为什么这份测试只守护 gert 体系，`ge::Tensor` 却不需要同类测试？

**参考答案**：`ge::Tensor` 对外类只有一个 `shared_ptr<Impl>` 成员（pimpl），真实字段全在 `.cc` 内部的 `Impl` 里，改 `Impl` 不影响对外布局，天然 ABI 安全，无需偏移级测试；gert 体系为了执行期性能选择全 POD 直接裸内存访问（无间接层、可跨进程），就必须用测试把每个字节的排布钉死。两种策略是性能与维护成本之间的取舍，不是疏漏。

**练习 3**：如果某次改动真的需要切用 `Shape::reserved_` 的前 8 字节，改动者必须同步更新哪些东西？

**参考答案**：至少四处——`shape.h` 中 `reserved_` 的定义/注释（或切出显式命名的字段并压缩 reserved）；ABI 测试中 `kReservedFieldSize` 相关断言（此时 `reserved_` 剩 32 字节，`sizeof(reserved_)` 断言与总大小断言需重新核对，本例总大小不变仍为 248）；新字段的取值语义文档（老版本 so 看到的是未初始化的保留区，必须定义默认值或版本协商机制，参考 `Tensor` 的 `version_` 字段用法）；以及按 README 流程确认这是「跨仓公共需求」。切保留段不破坏布局，但**新字段在旧版本二进制中的取值**是新的兼容性问题，不能只看 sizeof 不变就认为万事大吉。

## 5. 综合实践

**任务：为「gert 结构体加字段」做一次完整的 ABI 影响评估演练。**

背景：假设产品需要给 `ComputeNodeInfo` 增加一个 `uint32_t cascade_level_` 字段（表示节点的级联层级）。请你：

1. **定位契约数字**：打开 [tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc:151-L171](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/abi_compatibility_for_exe_graph_unittest.cc#L151-L171)，抄下当前被钉死的全部数字（总大小 88、`place_holder` 偏移 80、保留段 24）。
2. **列出三个候选方案**：末尾追加成员 / 中间插入 / 从 24 字节保留段切 4 字节，逐一判断哪些会触发哪条断言（对应本讲 4.3.2 的决策流程）。
3. **写出正确方案的落地步骤**：给出修改点清单（`compute_node_info.h` 的字段定义、ABI 测试中 `kComputeNodeInfoReservedFieldSize` 从 24 改 20 之类的联动、老版本二进制读到保留区时的默认值约定），并说明为什么总大小 88 保持不变意味着新旧 so 可以混布。
4. **验证**：在本地丢弃分支上按你的方案改一遍，`bash tests/run_test.sh -u` 跑 ABI 用例，确认只有你预期中的断言需要更新、其余 24 个用例不受影响（待本地验证）。

这个任务把你从「看懂测试」推进到「能在 ABI 约束下正确地改结构体」——这是 metadef 二开评审中最核心的能力。

## 6. 本讲小结

- ABI 兼容是「不重新编译依赖方、二进制直接互操作」的兼容性；metadef 被大量预编译组件依赖，README 把「保持 ABI 兼容」列为硬性贡献约束。
- metadef 用三道分层防线守护 gert 体系布局：头文件末尾 27 处 `static_assert(is_standard_layout)` 拦编译期破坏（如加虚函数）；ABI 单测的 `sizeof` 断言拦总大小变化；逐成员 `reinterpret_cast` 偏移断言拦内部错位。
- `abi_compatibility_for_exe_graph_unittest.cc` 把 25 个结构体的精确字节数（Shape=248、Tensor=752、各上下文=48 等）固化为常量契约；上下文继承链七个子类 sizeof 全部等于 48，用测试钉死了「派生类零新增成员」的设计纪律。
- 结构体尾部的 `reserved_` 字段（多为 40 字节）是预留的演进弹药：新字段从保留段切出，总大小与既有偏移不变，即 ABI 安全。
- ge 老体系走另一条路：pimpl（`ge::Tensor`）与 `AnyValue` 16 字节类型擦除，用间接层天然隔离布局，故不需要偏移级测试；gert 为执行期性能选择全 POD，就必须以测试换稳定。
- ABI 演进的正解是**并列新版本**（`TensorV2`、`OpImplRegisterV2`、`GetOutput2`），而不是修改旧结构；「尾部追加、老取值不复用」是贯穿枚举、槽位、函数集的统一原则。

## 7. 下一步学习建议

- 下一讲 u5-l5「单元测试体系与测试技巧」将展开 `tests/ut` 的整体组织：本讲出现的 `ut_metadef` glob 收集、run_test.sh 的 `-u` 模式、stub 机制都将在那里系统化，你还将学到如何新增一个能被 ctest 跑到的测试文件。
- 建议继续阅读的源码：`inc/external/exe_graph/runtime/` 目录下任意头文件末尾的 static_assert（体会守护的普遍性），以及 `runtime_tensor.h` 中 `Tensor` 与 `TensorV2` 并列共存的方式（体会「新版本并列」演进范式的完整实例）。
- 若想深入 ABI 理论，可对照阅读 C++ 标准中关于 layout compatibility 与 common initial sequence 的条款，再回头看 `static_assert(is_standard_layout)` 恰好是编译器能自动检查的那一部分——剩下的（偏移、对齐）正是 ABI 单测存在的理由。
