# 目录结构与头文件布局：inc、base、pkg_inc 的分工

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `inc/external`、`inc/register`、`inc/common`、`inc/base`、`pkg_inc`、`base`、`tests` 各目录的职责边界。
2. 根据一个头文件所在的路径，推断它的**可见范围**（谁能 include 它）和**稳定性等级**（改它会不会破坏下游）。
3. 对 `inc/external/graph/types.h` 中声明的任意一个接口，在 `base/` 目录下找到它的实现文件，并能说明中间经过了哪些「桥接头文件」。

承接前两讲：第 1 讲我们知道了 metadef 是 CANN 的基础组件库、被 ge 和各算子仓依赖；第 2 讲我们知道了 `build.sh` 编译产出四个库目标。本讲回答的问题是：**这几百个文件是怎么组织的？我拿到一个函数名，去哪里找它的声明和实现？**

## 2. 前置知识

### 2.1 头文件与实现分离

C++ 工程通常把「声明」（`.h` 头文件）和「实现」（`.cc` 源文件）分开。使用者只 include 头文件，链接时再去找实现所在的库。metadef 把这件事做到了极致：**头文件目录和实现目录是两个完全独立的顶层目录**，中间靠 CMake 的 include 路径串起来。

### 2.2 为什么头文件路径代表「契约强度」

metadef 被 ge、四个算子仓等**已经编译好的二进制**依赖。这意味着：

- 对外头文件里一个结构体的字段顺序、一个枚举的取值，都是**二进制契约**（ABI）的一部分，改了就可能导致下游崩溃而不是编译报错。
- 因此「这个头文件放在哪个目录」不是风格问题，而是在声明「我承诺它有多稳定」。

### 2.3 三个稳定等级（本讲的核心心智模型）

| 目录 | 谁能用 | 稳定性承诺 | 类比 |
|------|--------|-----------|------|
| `inc/external/` | 所有下游仓 + 外部用户 | 最高，改动需评估 ABI | 「公开 API」 |
| `inc/`（其余子目录，如 `inc/register`、`inc/base`、`inc/common`） | metadef 内部及 CANN 内部组件 | 中等，随版本可调整 | 「内部接口」 |
| `pkg_inc/` | 打包进 CANN 发布包的头文件 | 发布后冻结，含兼容转发层 | 「发布快照 + 迁移缓冲」 |

`base/` 是实现源码（`.cc`），`tests/` 是测试，两者不对外。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|-----------|------|
| `inc/external/graph/types.h` | 对外头文件代表：`ge::DataType`、`ge::Format` 枚举与类型工具函数声明 |
| `inc/register/op_impl_registry_api.h` | 内部接口代表：算子实现注册的 C 接口（`extern "C"`） |
| `inc/base/type/types_impl.h` | 桥接头文件：声明内部实现类 `TypeImpl` |
| `base/type/types_impl.cc` | 实现源码代表：`GetFormatName`、`GetSizeInBytes` 等的真正实现 |
| `base/utils/type_utils_impl.cc` | `TypeUtils::GetDataTypeLength` 的实现 |
| `pkg_inc/graph/type_utils.h` | pkg_inc 兼容层代表：弃用路径的转发头文件 |
| `inc/CMakeLists.txt` | 把各头文件目录注册为 include 路径的 CMake 脚本 |
| `base/host.cmake` | 把 `base/` 下源文件组织成库目标的 CMake 脚本 |

## 4. 核心概念与源码讲解

### 4.1 头文件目录总览：inc 与 pkg_inc 的分层

#### 4.1.1 概念说明

metadef 仓库顶层与代码相关的目录有 6 个：

```text
metadef/
├── inc/        头文件（按可见范围再分层）
│   ├── external/   ← 对外稳定接口（graph、exe_graph、register、asc、ge_common...）
│   ├── register/   ← 内部：算子实现注册的桥接层头文件
│   ├── common/     ← 内部：plugin、util、ge_common 等内部公共头
│   ├── base/       ← 内部：实现类的声明（如 base/type/types_impl.h）
│   └── graph/      ← 内部：graph 内部工具头
├── pkg_inc/    打包发布头文件（base、common、exe_graph、graph 四个子目录）
├── base/       实现源码（type、utils、registry、runtime、context_builder、asc...）
├── tests/      单元测试
├── example/    官方示例
└── docs/       文档
```

关键区分是 `inc/external` 与 `inc` 其余子目录：

- `inc/external/`：对外。子目录按「板块」组织——`graph/`（老图编译体系，`ge` 命名空间）、`exe_graph/runtime/`（新执行图运行时，`gert` 命名空间）、`register/`、`asc/register/`（算子原型定义）。这正是第 1 讲提到的「gert/ge 双体系」在目录上的投影。
- `inc/register/`、`inc/common/`、`inc/base/`、`inc/graph/`：metadef 内部（以及 CANN 内部兄弟组件）使用，外部工程不应 include。

#### 4.1.2 核心流程

这些目录如何被编译系统「接线」：

1. `inc/CMakeLists.txt` 定义一个 `INTERFACE` 库 `metadef_headers`，它不产出任何 `.so`，只携带 include 搜索路径。
2. `$<BUILD_INTERFACE:...>` 是**本仓库编译时**的路径（直接指向源码树里的 `inc/`、`pkg_inc/` 等）。
3. `$<INSTALL_INTERFACE:...>` 是**安装后**的路径（下游仓通过安装包里的相对路径 `include/metadef/...`、`pkg_inc/...` 找到同一批头文件）。
4. `base/` 下的所有库目标（`metadef`、`opp_registry`、`exe_graph` 等）都 `PUBLIC` 链接 `metadef_headers`，因此下游只需链接任意一个 metadef 库即可拿到全部头文件路径。

#### 4.1.3 源码精读

**`inc/CMakeLists.txt`：头文件路径的唯一登记处**。[inc/CMakeLists.txt:L12-L26](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/CMakeLists.txt#L12-L26) 定义了 INTERFACE 库并登记 BUILD 期的 include 路径——注意 `inc/external`、`inc/register`、`pkg_inc` 都在列表里，且有一行中文注释标明某些路径「非法，需要在后续整改中删掉」（[inc/CMakeLists.txt:L35-L40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/CMakeLists.txt#L35-L40)），说明目录边界仍在治理中。

**`inc/register/op_impl_registry_api.h`：内部接口的典型样貌**。这个文件在 `inc/register/`（而非 `inc/external/register/`），它是给 CANN 内部「宿主程序」拉取已注册算子实现用的 C 接口：[inc/register/op_impl_registry_api.h:L27-L39](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_api.h#L27-L39) 用 `extern "C"` 暴露了三个函数（`GetRegisteredOpNum`、`GetOpImplFunctions`、`GetOpImplFunctionsV2`），并用 `METADEF_FUNC_VISIBILITY`（即 `__attribute__((visibility("default")))`，定义见 [L31-L35](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_api.h#L31-L35)）保证符号从 `.so` 中导出。它 include 的 `register/op_impl_kernel_registry.h` 则位于 `inc/external/register/`——**内部头依赖对外头，方向不能反过来**。

#### 4.1.4 代码实践

**实践：用目录清单验证「板块 → 目录」的映射。**

1. 实践目标：建立「看到 include 路径就能说出它属于哪个板块、多稳定」的直觉。
2. 操作步骤：
   ```bash
   ls inc/external/          # 对外板块一览
   ls inc/external/exe_graph/runtime/ | head -20   # gert 运行时头文件
   ls inc/register/          # 内部注册桥接层
   ls pkg_inc/graph/         # 发布包头文件
   ```
3. 需要观察的现象：`inc/external/exe_graph/runtime/` 下能看到 `kernel_context.h`、`tiling_context.h`、`shape.h` 等（单元三的主角）；`inc/register/` 下只有 6 个头文件，全部与「算子实现注册」相关。
4. 预期结果：`inc/external` 的子目录名（graph、exe_graph、register、asc）与 README 中 API 文档的板块划分一一对应。

#### 4.1.5 小练习与答案

**练习 1**：`inc/common/plugin/plugin_manager.h` 和 `inc/external/graph/types.h`，哪个改动对下游用户风险更大？

**答案**：`types.h` 风险更大。它在 `inc/external/`，是对外稳定契约，其枚举值、结构体布局参与下游已编译二进制的 ABI；`plugin_manager.h` 在 `inc/common/`，属内部接口，外部不应直接依赖。

**练习 2**：为什么 `inc/CMakeLists.txt` 里同一批目录要写两遍（BUILD_INTERFACE 和 INSTALL_INTERFACE）？

**答案**：因为编译时头文件在源码树（`${METADEF_DIR}/inc/...`），安装后在安装包的相对路径（`include/metadef/...`）。CMake 的生成器表达式保证同一个 `metadef_headers` 目标在本仓库构建和被下游导入时各自使用正确的路径。

### 4.2 从声明到实现：以 types.h → types_impl.cc 为例

#### 4.2.1 概念说明

这是本讲最重要的最小模块：**一次完整的「接口查找」旅程**。

`inc/external/graph/types.h` 是 metadef 被引用最广的头文件之一，定义了 `ge::DataType`、`ge::Format` 两大枚举和一批工具函数。观察它会发现一个有趣的现象——文件里的函数分两类：

- **`inline` 函数**：直接在头文件里给出实现（如 `GetSizeByDataType`），编译进每个使用者，没有对应的 `.cc`。
- **只有声明、标着可见性宏的函数**：如 `GetSizeInBytes`、`GetFormatName`，实现在 `base/` 目录里，编译进 `libmetadef.so`。

而实现并不是直接写一个同名函数完事：中间还有一层**桥接头文件** `inc/base/type/types_impl.h`，把实现包进 `TypeImpl` 类。这样做的目的：`types.h` 保持纯净的对外声明，实现细节（日志、内部工具依赖）全部关在 `base/` 一侧。

#### 4.2.2 核心流程

以「使用者调用 `ge::GetSizeInBytes(100, ge::DT_FLOAT)`」为例的完整链路：

```text
使用者源码
  │  #include "graph/types.h"            （对外声明，见 inc/external/graph/types.h:190）
  ▼
链接期符号 _ZN2ge12GetSizeInBytesExNS_8DataTypeE
  │  由 libmetadef.so 提供
  ▼
base/type/types_impl.cc:179  ge::GetSizeInBytes(...)        ← 入口薄封装
  │  直接转发
  ▼
base/type/types_impl.cc:115  TypeImpl::GetSizeInBytes(...)  ← 真正实现
  │  内部又调用
  ▼
base/utils/type_utils_impl.cc:327  TypeUtils::GetDataTypeLength(...) ← 跨文件依赖
```

同时，CMake 侧的接线在 `base/host.cmake`：源文件列表 `SRC_LIST` 明确列出 `"type/types_impl.cc"`，并以此构建 `libmetadef.so`。

#### 4.2.3 源码精读

**第一步：对外声明**。[inc/external/graph/types.h:L186-L192](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L186-L192) 声明了 `GetSizeInBytes`（只有声明、无函数体、带 doxygen 注释），紧接着是 `Format` 枚举。作为对比，`GetSizeByDataType` 是 [inc/external/graph/types.h:L133-L184](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L133-L184) 的 `inline` 函数，用静态查表实现，**不需要去 base/ 找实现**。另一批 `inline` 位运算工具 `GetPrimaryFormat`/`GetSubFormat` 见 [inc/external/graph/types.h:L279-L289](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L279-L289)，把一个 int32 拆成主格式/子格式（位域布局见 [L252-L258](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L252-L258) 的注释图）。

**第二步：桥接头文件**。[inc/base/type/types_impl.h:L13-L23](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/base/type/types_impl.h#L13-L23) include 了对外头 `"external/graph/types.h"`（注意 include 前缀变了，因为 CMake 把 `inc/` 本身加进了搜索路径），然后声明内部类 `TypeImpl`，聚集了 `GetFormatName`、`GetSizeInBytes` 等静态方法。这个文件在 `inc/base/`，属于「实现侧的声明」，外部不可见。

**第三步：实现**。[base/type/types_impl.cc:L115-L139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L115-L139) 是 `TypeImpl::GetSizeInBytes` 的真正实现：处理负数元素个数、位单位类型（`kDataTypeSizeBitOffset` 偏移）的向上取整、乘法溢出检查。注意它 include 了 `"common/ge_common/debug/ge_log.h"`（[L14](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L14)）——这类对内部日志组件的依赖正是**不能**直接写在对外头文件里的原因。

**第四步：入口薄封装**。[base/type/types_impl.cc:L175-L181](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/type/types_impl.cc#L175-L181) 提供 `ge::GetFormatName` 和 `ge::GetSizeInBytes` 两个 `ge` 命名空间下的自由函数，一行转发到 `TypeImpl`。使用者链接的符号由这里产生。

**第五步：CMake 接线**。[base/host.cmake:L216-L246](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/host.cmake#L216-L246) 的 `SRC_LIST` 列出 `type/types_impl.cc`、`utils/type_utils_impl.cc` 等，随后 `add_library(metadef SHARED ${SRC_LIST})` 把它们编进 `libmetadef.so`。`base/` 子目录与源文件的对应关系由此一目了然：`base/type/` 放类型实现、`base/utils/` 放工具实现、`base/registry/` 放注册实现……

#### 4.2.4 代码实践

**实践：亲手完成一次「声明 → 实现」追踪。**

1. 实践目标：以 `ge::GetFormatName` 为目标，走完「对外声明 → 桥接头 → 实现 → 库归属」四步，并整理成树状图。
2. 操作步骤：
   ```bash
   # ① 在对外头文件中找到声明（无函数体）
   grep -n "GetFormatName" inc/external/graph/types.h
   # ② 在 base/ 中找到实现
   grep -rn "GetFormatName" base/
   # ③ 确认桥接头文件
   cat inc/base/type/types_impl.h
   # ④ 确认实现被编进哪个库
   grep -n "types_impl.cc" base/host.cmake
   ```
3. 需要观察的现象：`grep` 在 ① 只命中一处声明（types.h 第 336 行）；在 ② 命中实现文件 types_impl.cc 的两处（`TypeImpl::GetFormatName` 第 19 行、`ge::GetFormatName` 封装第 175 行）；④ 显示该文件属于 `metadef` 库的源文件列表。
4. 预期结果：整理出如下树状图（示例）：

   ```text
   inc/external/graph/types.h        声明：GE_FUNC_*_VISIBILITY const char_t *GetFormatName(Format)
     └── inc/base/type/types_impl.h   桥接：class TypeImpl { static GetFormatName(...) }
           └── base/type/types_impl.cc 实现：TypeImpl::GetFormatName（查 names 静态表）
                 └── base/host.cmake   归属：SRC_LIST → libmetadef.so
   ```

5. 本实践为纯源码阅读型，不依赖昇腾环境，可直接执行。

#### 4.2.5 小练习与答案

**练习 1**：`ge::GetPrimaryFormat`（types.h 第 279 行）需要去 `base/` 找实现吗？为什么？

**答案**：不需要。它是头文件内的 `inline` 函数，实现在 [inc/external/graph/types.h:L279-L281](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L279-L281) 就已完整给出（一次位与运算），会直接内联到调用方。只有「声明无体」的函数才需要去实现目录找。

**练习 2**：为什么 `types_impl.cc` 不直接在 `ge::GetSizeInBytes` 里写全部逻辑，而要转发给 `TypeImpl` 类？

**答案**：分层解耦。对外头文件只暴露自由函数签名；实现类 `TypeImpl` 定义在 `inc/base/type/types_impl.h`，可以自由引用日志、内部工具等实现侧依赖，而不把这些依赖泄漏进 `inc/external` 的契约。这也是 metadef 全仓库通用的「Impl 后缀类 + 薄封装」模式。

**练习 3**：`TypeImpl::GetSizeInBytes`（types_impl.cc 第 115 行）内部调用了 `TypeUtils::GetDataTypeLength`，这个函数的声明和实现分别在哪？

**答案**：声明在 [inc/external/graph/utils/type_utils.h:L27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/utils/type_utils.h#L27)（`TypeUtils` 公共类），实现在 [base/utils/type_utils_impl.cc:L327-L329](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/utils/type_utils_impl.cc#L327-L329)（转发到同文件第 387 行的 `TypeUtilsImpl`）。注意 `base/type/` 与 `base/utils/` 是两个不同实现子目录。

### 4.3 pkg_inc 兼容层：发布快照与弃用转发

#### 4.3.1 概念说明

`pkg_inc/` 的名字来自「package include」：这批头文件会随 CANN 安装包发布，供离线的算子开发场景使用。它有四个子目录（`base`、`common`、`exe_graph`、`graph`），内容上是「头文件自包含」的一批模板/内联设施，例如 `pkg_inc/graph/any_value.h`、`pkg_inc/graph/type_id.h`（第 2 单元会精读）。

本讲聚焦的 `pkg_inc/graph/type_utils.h` 展示了 pkg_inc 的另一项职责：**兼容转发层**。当某个头文件被重命名或迁移时，旧路径不能立刻删除（下游还在用），于是在旧位置放一个只做 `#include` 转发并打印弃用警告的壳文件。

#### 4.3.2 核心流程

兼容转发的生命周期：

1. 头文件从旧路径迁移到新路径（本例：`type_utils.h` 的内容并入 `type_id.h`）。
2. 旧路径保留壳文件：`#pragma message` 打印弃用警告 + include 新头。
3. 壳文件中写明计划删除时间（本例为 2027-06 之后）。
4. 到期后在某个大版本删除壳文件，仍引用旧路径的工程将编译失败。

#### 4.3.3 源码精读

**一个只有 21 行的完整兼容头**。[pkg_inc/graph/type_utils.h:L11-L21](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_utils.h#L11-L21) 全文三要素：独立头文件保护宏（`EXECUTE_GRAPH_TYPE_UTILS_H`）、`#pragma message` 弃用警告（第 14 行，提示改用 `"type_id.h"`）、一行 `#include "graph/type_id.h"`（第 15 行）指向同目录的接替者。注释明确说明这是「保持旧 include 路径的兼容头，计划 2027-06 后移除」。

**转发目标 `pkg_inc/graph/type_id.h`** 是真正的实现，[pkg_inc/graph/type_id.h:L18-L38](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/pkg_inc/graph/type_id.h#L18-L38) 定义了 `ge::TypeId`（`void *` 别名）、模板类 `TypeIdHolder<T>` 和 `GetTypeId<T>()`——第 2 单元讲 AnyValue 时会回到这里。

**pkg_inc 在构建中的接线**。[inc/CMakeLists.txt:L26-L33](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/CMakeLists.txt#L26-L33) 把 `pkg_inc` 及其子目录加入 BUILD 期搜索路径，[L54-L60](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/CMakeLists.txt#L54-L60) 对应安装期路径——所以源码里写 `#include "graph/type_id.h"` 时，实际命中的是 `pkg_inc/graph/type_id.h`（目录名 `pkg_inc` 不出现在 include 语句中）。

#### 4.3.4 代码实践

**实践：观察弃用警告的触发。**

1. 实践目标：亲眼看到兼容层的 `#pragma message` 在编译期输出。
2. 操作步骤（示例代码，非项目原有代码）：
   ```cpp
   // deprecation_probe.cpp —— 示例代码
   #include "graph/type_utils.h"   // 故意使用弃用路径
   int main() { return 0; }
   ```
   编译（需先完成第 2 讲的构建，或手动指定 include 路径）：
   ```bash
   g++ -I pkg_inc -I inc/external deprecation_probe.cpp -c -o /tmp/probe.o
   ```
3. 需要观察的现象：编译输出中出现 `Warning: type_utils.h is deprecated and will be removed after 2027-06. Include "type_id.h" instead.`
4. 预期结果：编译仍成功（壳文件只是转发），但警告提示迁移。待本地验证（若无 g++ 环境可改用第 2 讲的构建目录中任一目标触发）。

#### 4.3.5 小练习与答案

**练习 1**：`pkg_inc/graph/def_types.h` 和 `inc/external/graph/types.h` 都定义类型相关内容，为什么分属两个目录？

**答案**：`inc/external/` 的头文件随开发环境提供且是编译链接契约的一部分；`pkg_inc/` 的头文件进入 CANN 安装包，面向打包发布场景（多为头文件内即完整实现的模板/内联设施），发布后受更强的兼容约束。同为「类型」主题但服务不同分发渠道。

**练习 2**：如果 2027-06 后删除 `pkg_inc/graph/type_utils.h`，谁会编译失败？

**答案**：所有仍写 `#include "graph/type_utils.h"` 的下游工程（ge、算子仓或用户算子代码）——这正是弃用警告提前两年多出现的原因：给下游充足的迁移窗口，也体现了第 1 讲强调的 ABI/源码兼容责任。

## 5. 综合实践

**任务：为 metadef 建立一张「头文件目录 → 实现目录」对照表，并追踪一条完整调用链。**

1. 选定 3 个对外接口：本讲的 `ge::GetFormatName`、`ge::GetSizeInBytes`，以及 `ge::TypeUtils::GetDataTypeLength`。
2. 对每个接口完成 4.2.4 的四步追踪（声明位置 → 桥接头 → 实现文件 → 所属库目标）。
3. 把结果整理成一张 Markdown 表格，形如：

   | 对外接口 | 声明（inc/external） | 桥接头 | 实现（base/） | 所属库 |
   |---|---|---|---|---|
   | `ge::GetFormatName` | `graph/types.h:336` | `inc/base/type/types_impl.h` | `base/type/types_impl.cc:19` | `metadef` |
   | `ge::GetSizeInBytes` | `graph/types.h:190` | 同上 | `base/type/types_impl.cc:115` | `metadef` |
   | `TypeUtils::GetDataTypeLength` | `graph/utils/type_utils.h:27` | 无（Impl 同文件） | `base/utils/type_utils_impl.cc:327` | `metadef` |

4. 验证方法：在第 2 讲构建出的环境中写一个小测试程序（或直接阅读 `tests/ut/base/testcase/types_unittest.cc`），确认这三个接口都能被 `#include "graph/types.h"` + 链接 `libmetadef.so` 使用。
5. 这张表是你后续阅读任意 metadef 接口的通用方法论：**先看路径定稳定性，再找声明，再顺藤摸到 base/ 实现**。

## 6. 本讲小结

- metadef 顶层按 `inc`（头文件）、`pkg_inc`（发布头文件）、`base`（实现）、`tests`（测试）组织，**头文件路径即稳定等级**：`inc/external` 最稳、`inc/` 其余为内部、`pkg_inc` 为发布快照。
- 对外头文件中的函数分两类：`inline`（实现就在头里，如 `GetSizeByDataType`）和「声明无体」（要去 `base/` 找实现，如 `GetSizeInBytes`）。
- 声明到实现之间通常隔着一层 `inc/base/` 下的桥接头文件（如 `types_impl.h` 声明 `TypeImpl`），实现细节与内部依赖被关在 `base/` 一侧。
- `base/` 子目录按板块划分（`type`、`utils`、`registry`、`runtime`、`context_builder`、`asc`），由 `base/host.cmake` 的源文件列表编入 `metadef`、`opp_registry`、`exe_graph` 等库目标。
- `pkg_inc/` 承担发布与兼容职责，弃用路径通过「壳头文件 + `#pragma message`」渐进迁移（如 `type_utils.h` → `type_id.h`，计划 2027-06 后移除）。
- `inc/CMakeLists.txt` 的 INTERFACE 库是所有 include 路径的唯一登记处，用生成器表达式区分构建期与安装期路径。

## 7. 下一步学习建议

本讲你已掌握目录地图和「声明→实现」追踪方法，接下来进入**单元二：基础数据结构**：

- 下一讲 `u2-l1` 将精读本讲反复出现的 `types.h`，深入 `DataType`/`Format` 枚举体系与格式位域运算，可以把 [inc/external/graph/types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h) 完整通读一遍作为预习。
- 同时建议顺手阅读 `pkg_inc/graph/def_types.h` 与 `pkg_inc/graph/graph_type_utils.h`，对照本讲的 pkg_inc 概念。
- 想巩固本讲方法的读者，可以自行追踪 `ge::AscendString`（声明在 `inc/external/graph/ascend_string.h`）的实现位置，为 `u2-l2` 做准备。
