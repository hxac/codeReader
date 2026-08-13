# SuperKernel 目录结构与构建产物

## 1. 本讲目标

上一讲（u2-l1）我们理解了 SuperKernel「为什么存在」——把整网 N 个子算子缝合成一个超核，省下 N−1 次调度与同步开销，并叠加 ICache 预加载、Early-Start 等深度优化。本讲回答「它的代码长在哪、怎么被组织起来、最终编出什么产物」。

学完本讲，你应当能够：

1. 说出 `super_kernel/` 目录的源码分层：`src/jit`（Python 代码生成）、`src/aot`（C++ 运行时）、`include`（公共 C 头）、`kernel`（设备端算子源）。
2. 区分**编译期 JIT**（host 侧用 Python 生成代码）与**运行期 AOT**（设备上用 C++ 做图优化与下发）这两层，并指出它们的边界。
3. 看懂 CMake 里的两个核心构建目标：`ascendsk` 共享库（C++ 运行时）与 `superkernel_whl`（Python wheel 包），以及 `kernel/` 子目录如何把设备二进制嵌进运行时库。

## 2. 前置知识

- **JIT / AOT（在本项目中的特殊含义）**：传统上 JIT 指「运行时即时编译」，AOT 指「提前编译」。但 SuperKernel 里这两个词指的是**代码生成发生在哪一阶段**：
  - **JIT 层（本仓）= 编译期、host 侧、用 Python 生成 C++/设备源码**。它发生在「模型被编译成可执行图」的时候，由 Python 包 `superkernel` 完成。
  - **AOT 层（本仓）= 运行期、设备侧、用 C++ 做图优化与下发**。它发生在「图已经被下发、即将在 NPU 上执行」的时候，编进共享库 `libascendsk.so`。
  - 这两层的命名容易和教科书定义打架，记住「**Python 写代码 vs C++ 跑运行时**」即可。
- **共享库（shared library）**：Linux 下的 `.so` 文件，可以被多个进程加载。`libascendsk.so` 就是 SuperKernel 的运行时库。
- **wheel（`.whl`）**：Python 的标准安装包格式。`superkernel-0.1.0-py3-none-any.whl` 就是把 Python JIT 包打包分发用的产物。
- **fatbin / `.o`**：设备算子源（`.asc`，AscendC 源码）经 ASC 编译器编出的设备二进制（fat binary）。本讲会看到它如何被「嵌」进 C++ 源码。
- 如果你对顶层目录划分还不熟，建议先回看 u1-l2「仓库目录结构与组件关系」。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从外到内」排列：

| 文件 | 作用 |
|------|------|
| [super_kernel/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/CMakeLists.txt) | 组件总装配，定义 `superkernel_whl` 与 `ascendsk` 两个核心目标，并 `add_subdirectory(kernel)` |
| [super_kernel/include/super_kernel/super_kernel.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h) | 唯一对外公开的 C 头，声明 `aclsk*` 运行时接口与选项结构（JIT/AOT 的公共契约） |
| [super_kernel/src/jit/superkernel/super_kernel_constants.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_constants.py) | JIT 侧常量与枚举（设备类型、超核类型、Early-Start 模式等） |
| [super_kernel/src/jit/superkernel/super_kernel.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py) | JIT 入口，`compile()` 负责生成融合超核源码 |
| [super_kernel/src/aot/super_kernel.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp) | AOT 入口，实现 `aclskOptimize` 等 C 接口 |
| [super_kernel/kernel/CMakeLists.txt](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/kernel/CMakeLists.txt) | 设备算子构建：按平台编 `sk_entry`/`sk_scope`，并把二进制生成 C++ stub |
| [super_kernel/scripts/gen_sk_entry_stub.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/scripts/gen_sk_entry_stub.py) | 把设备 `.o` 二进制读成 C++ 数组、嵌进运行时库的脚本 |
| [super_kernel/pyproject.toml](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/pyproject.toml) | Python 打包配置，说明 wheel 里 `superkernel` 包映射到哪个源码目录 |

## 4. 核心概念与源码讲解

### 4.1 JIT 与 AOT 分层

#### 4.1.1 概念说明

SuperKernel 是一个**双层结构**，两层的语言、运行时机、产物都不同：

- **JIT 层（Python，编译期）**：位于 `super_kernel/src/jit/superkernel/`，是一个标准 Python 包。它在「模型被编译成图」时运行，读入算子元数据（`op_infos` / `sub_op_infos`），**生成**融合后的 SuperKernel C++/设备源码。换句话说，它「写代码」。
- **AOT 层（C++，运行期）**：位于 `super_kernel/src/aot/`，编进 `libascendsk.so`。它在「图即将在 NPU 上执行」时运行，做图优化、scope 调度、超核下发。换句话说，它「跑运行时」。

为什么要分两层？因为「生成代码」和「在设备上优化调度」是两个截然不同的工作：前者需要灵活的字符串/模板拼接，Python 更顺手；后者需要极致的性能和与 ACL runtime 的 C ABI 对接，C++ 更合适。两层各司其职，中间靠一个公共 C 头对接（见 4.2）。

#### 4.1.2 核心流程

```text
【编译期 · JIT 层】
  super_kernel.py: compile(kernel_infos)
     │  读 op_infos / sub_op_infos（算子元数据）
     │  拼接生成融合超核的 C++ / .asc 设备源码
     ▼
  ASC 编译器把设备源码编成 fatbin（.o 二进制）

【运行期 · AOT 层】
  外部 ACL runtime 调用 aclskOptimize(model, options)
     │  （libascendsk.so 导出的 C 接口）
     ▼
  SuperKernelGraph.InitSKGraph()   —— 构建运行时图
     ▼
  SuperKernelOptimizer.Process()   —— 融合 / scope 切分优化
     ▼
  graph.Update()                   —— 回写优化结果，准备下发超核
```

关键点：**JIT 在编译期把「超核长什么样」生成出来，AOT 在运行期决定「这些超核怎么被融合、调度、下发」**。两者职责清晰，互不越界。

#### 4.1.3 源码精读

**JIT 入口签名**——`compile()` 接收算子信息列表，负责生成融合超核代码：

```python
def compile(kernel_infos, called_kernel_name="ascendc_super_kernel_plus", compile_infos=None):
```

> 见 [super_kernel/src/jit/superkernel/super_kernel.py:L1044](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1044)。`kernel_infos` 描述要融合的子算子，`called_kernel_name` 是生成出的超核入口名。

**JIT 侧如何描述「超核类型」**——常量文件里用枚举列出所有 Kernel Type，这是 JIT 生成代码时要写到设备源码里的关键信息：

```python
class SuperKernelKernelType(enum.Enum):
    """super kernel kernel type."""
    KERNEL_TYPE_AIV_ONLY = 0      # 纯 Vector 核
    KERNEL_TYPE_AIC_ONLY = 1      # 纯 Cube 核
    KERNEL_TYPE_MIX_AIV_HARD_SYNC = 2
    ...
    KERNEL_TYPE_AICORE = 8
    KERNEL_TYPE_MAX = 12
```

> 见 [super_kernel/src/jit/superkernel/super_kernel_constants.py:L97-L111](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_constants.py#L97-L111)。`AIV`（AI Vector）/`AIC`（AI Cube）/`MIX` 三类对应上一讲提到的「按 Kernel Type 缩小同步范围」优化。

**AOT 入口实现**——`aclskOptimize` 是运行期的总入口，开头做环境初始化、构建图，随后调用优化器：

```cpp
aclError aclskOptimize(aclmdlRI model, aclskOptions *options) {
  SkModelContext modelContext(model);
  InitSkLogger(GetCurrentModelLabel());
  InitSkRuntimeConfig();
  ...
}
```

> 见 [super_kernel/src/aot/super_kernel.cpp:L80-L92](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp#L80-L92)。

真正做融合优化的地方在更靠后的位置：

```cpp
SuperKernelOptimizer optimizer(opts);
if (!optimizer.Process(graph)) {
  SK_LOGE("aclskOptimize failed: optimize sk graph failed");
  return ACL_ERROR_FAILURE;
}
```

> 见 [super_kernel/src/aot/super_kernel.cpp:L147-L153](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp#L147-L153)。`SuperKernelOptimizer::Process` 就是上一讲说的「运行期图优化」主流程，它的内部细节留到 u10-l2「SkOptimizer 与 SkTaskBuilder 融合决策」精读。

#### 4.1.4 代码实践

**实践目标**：亲手定位 JIT Python 包与 AOT C++ 源的路径，并指出二者对接的公共头。

**操作步骤**：

1. 在仓库根目录执行 `ls super_kernel/src/jit/superkernel/`，确认这是一个 Python 包（有 `__init__.py`），列出其中的 `.py` 文件。
2. 执行 `ls super_kernel/src/aot/`，确认这是 C++ 源目录（一堆 `sk_*.cpp` / `sk_*.h`）。
3. 打开 `super_kernel/src/aot/super_kernel.cpp`，在文件顶部用 `grep -n '#include'` 查看它 include 了哪些头；你会发现它并没有直接 include JIT 的 Python——这正是「两层语言不同、靠 C ABI 对接」的体现。
4. 真正的对接点是公共 C 头：`super_kernel/include/super_kernel/super_kernel.h`（详见 4.2）。AOT 在这里实现 `aclskOptimize` 等函数，外部 runtime 通过这个头调用它们。

**需要观察的现象**：

- JIT 目录是 `.py`，AOT 目录是 `.cpp/.h`，两者**没有任何源码级 import/include 关系**——它们是靠「生成的设备源码 + 公共 C 头」间接对接的。

**预期结果**：

- JIT Python 包路径：`super_kernel/src/jit/superkernel/`
- AOT C++ 源路径：`super_kernel/src/aot/`
- 公共对接头：`super_kernel/include/super_kernel/super_kernel.h`（声明 `aclsk*` C 接口）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `src/jit` 整个目录删掉，`libascendsk.so` 还能编出来吗？为什么？

**参考答案**：能。`libascendsk.so`（`ascendsk` 目标）只依赖 `src/aot/*.cpp` 和 `kernel/` 生成的 stub（见 4.3），不依赖 Python 源。删掉 JIT 后只是「编译期不再生成超核源码」，运行时库本身照常构建。

**练习 2**：`SuperKernelKernelType` 里 `KERNEL_TYPE_VECTORCORE = 9` 的注释写着 `# donot use in sk`，这说明什么？

**参考答案**：说明这个枚举是和外部共享的类型集合里「借来」的，但 SuperKernel 实际不使用该值。读源码时遇到这类标注要留意，避免误以为它会参与融合逻辑。

---

### 4.2 include 公共 C 接口

#### 4.2.1 概念说明

`super_kernel/include/super_kernel/super_kernel.h` 是整个组件**唯一对外公开的 C 头**。它的作用是定义一个稳定的 **C ABI 契约**：

- 对**外部调用方**（ACL runtime、aclgraph 等）：它声明了 `aclskOptimize`、`aclskScopeBegin`、`aclskScopeEnd` 等入口函数，外部只需要 `#include` 这个头、链接 `libascendsk.so` 就能驱动 SuperKernel。
- 对**内部 AOT 实现**：AOT 的 `super_kernel.cpp` 必须 `extern "C"` 地实现这些函数，签名、结构体布局都要和头里一字不差。

为什么要用纯 C 接口（`extern "C"`）而不是 C++ 符号？因为 C ABI 跨编译器、跨语言稳定，调用方未必是 C++（可能是 runtime 的 C 代码）。头里用 `ACL_FUNC_VISIBILITY`（展开为 `__attribute__((visibility("default")))`）显式导出符号，确保这些函数能被动态加载到。

#### 4.2.2 核心流程

```text
super_kernel.h（公共契约）
   ├── 选项类型：aclskOptionType 枚举（PRELOAD_CODE / EARLY_START / ...）
   ├── 选项容器：aclskOption（联合体）+ aclskOptions（数组）
   └── 三个核心函数声明：
         aclskOptimize(model, options)   —— 整图超核优化
         aclskScopeBegin(name, stream)   —— 标记 scope 区域起点
         aclskScopeEnd(name, stream)     —— 标记 scope 区域终点
                       │
                       ▼  （extern "C" 实现，符号导出）
              libascendsk.so（src/aot/super_kernel.cpp 等）
```

#### 4.2.3 源码精读

**选项类型枚举**——把 SuperKernel 的所有可调开关编成号，调用方按号传参：

```cpp
enum class aclskOptionType : uint32_t {
  PRELOAD_CODE = 0,        // ICache 预加载
  SPLIT_MODE = 1,          // 子核拆分模式
  CONSTANT_CODEGEN = 6,    // 常量化代码生成
  EARLY_START = 16,        // Early-Start 全局开关
  SK_OPTION_MAX
};
```

> 见 [super_kernel/include/super_kernel/super_kernel.h:L41-L61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L41-L61)。这里的 `EARLY_START`、`SPLIT_MODE` 正是上一讲提到的四项深度优化里的两项。

**选项容器**——用联合体把所有选项结构体打包，外部传一个数组进来：

```cpp
struct aclskOption {
  aclskOptionType optionType;
  union {
    aclskPreloadOption preload;
    aclskSplitModeOption splitMode;
    aclskEarlyStartOption earlyStart;
    ...
  };
};
typedef struct aclskOptions {
  aclskOption *options;
  size_t numOptions;
} aclskOptions;
```

> 见 [super_kernel/include/super_kernel/super_kernel.h:L167-L194](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L167-L194)。这种「枚举 + 联合体」是 C 接口里常见的可扩展选项传参手法。

**三个核心函数声明**——这是对外暴露的全部运行时入口：

```cpp
ACL_FUNC_VISIBILITY aclError aclskOptimize(aclmdlRI model, aclskOptions *options);
ACL_FUNC_VISIBILITY aclError aclskScopeBegin(const char *scopeName, aclrtStream stream);
ACL_FUNC_VISIBILITY aclError aclskScopeEnd(const char *scopeName, aclrtStream stream);
```

> 见 [super_kernel/include/super_kernel/super_kernel.h:L261-L263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L261-L263)。`aclskOptimize` 对应「整图优化」，`aclskScopeBegin/End` 对应「显式标记一个 scope 区域」（scope 是用户手动圈定的一组算子，详见 u10-l3）。

这些声明在 AOT 侧被实现，例如：

```cpp
aclError aclskOptimize(aclmdlRI model, aclskOptions *options) { ... }
```

> 见 [super_kernel/src/aot/super_kernel.cpp:L80](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp#L80)。头里的声明与这里的实现签名完全一致，这就是「契约」的落点。

#### 4.2.4 代码实践

**实践目标**：验证「公共头 = 对外契约」这一论断，并建立 C 头与 JIT 常量的对照。

**操作步骤**：

1. 在 `super_kernel/include/super_kernel/super_kernel.h` 中数一下 `ACL_FUNC_VISIBILITY` 出现的次数——它标注的都是对外导出的 C 函数。
2. 用 `grep -rn 'aclskOptimize' super_kernel/src/aot/` 找到实现位置，确认签名与头一致。
3. 对照 C 头里的 `aclskScopeVerifyKernelType`（VECTOR=2/CUBE=1/MIX=3）与 JIT 常量 `super_kernel_constants.py` 里的 `SuperKernelDeviceType`（AIV=0/AIC=1/MIX=2）。你会发现两侧都在描述「核类型」，但**编号不同**——这正是为什么需要公共头来约束边界。

**需要观察的现象**：

- 头里导出的函数数量很少（只有几个 `aclsk*`），说明对外接口刻意保持精简。
- 两侧核类型枚举编号不一致，说明它们是各自独立的抽象，不能混用。

**预期结果**：

- 公共头只导出极少数 C 函数，是稳定 ABI 的体现。
- 修改 `aclsk*` 函数签名属于「破坏 ABI」的改动，需极其谨慎（这正是 u12-l3「编码红线」会强调的内容）。

#### 4.2.5 小练习与答案

**练习 1**：为什么选项传参用「枚举 + 联合体」的 `aclskOption`，而不是给每个开关写一个独立函数？

**参考答案**：为了在不破坏 ABI 的前提下扩展。新增一个选项只需加一个枚举值和一个联合体成员，老的 `aclskOptimize` 签名不变；若每个开关一个函数，接口会爆炸式增长，且增删都会改变符号表。

**练习 2**：头文件里 `extern "C"` 包裹的作用是什么？去掉会怎样？

**参考答案**：`extern "C"` 强制使用 C 链接（不做 C++ name mangling），保证符号名就是 `aclskOptimize` 本身。去掉后 C++ 编译器会把名字改写成带参数类型的符号（如 `_Z13aclskOptimize...`），外部 C 调用方就找不到这个符号了。

---

### 4.3 CMake 目标与 wheel 产物

#### 4.3.1 概念说明

`super_kernel/CMakeLists.txt` 定义了**两个核心构建目标**，正好对应 JIT 与 AOT 两层：

| 目标 | 类型 | 产物 | 对应层 |
|------|------|------|--------|
| `superkernel_whl` | CMake 自定义目标（custom target） | `superkernel-0.1.0-py3-none-any.whl` | JIT 层（Python 包） |
| `ascendsk` | SHARED 共享库 | `libascendsk.so` | AOT 层（C++ 运行时） |

此外还有一个 `kernel/` 子目录，它产出**设备端算子目标**（`sk_entry_*`、`sk_scope_*`），这些目标会被「嵌」进 `ascendsk` 库。理解这三个角色的依赖关系，就看懂了 SuperKernel 的构建全貌。

#### 4.3.2 核心流程

```text
super_kernel/CMakeLists.txt
   │
   ├── 【目标1】superkernel_whl  ── python3 -m build --wheel ──►  superkernel-0.1.0-py3-none-any.whl
   │        （把 src/jit/superkernel/ 打成 wheel，install 进 .run 包）
   │
   ├── add_subdirectory(kernel)   ──►  kernel/CMakeLists.txt
   │        │
   │        ├── 按 dav-2201 / dav-3510 两个平台，各编一个 sk_entry_<arch>（fatbin）
   │        ├── 各编一个 sk_scope_<arch>（静态库）
   │        └── gen_sk_entry_stub.py 把 sk_entry 的 .o 二进制读成 C++ 数组
   │            生成 sk_kernel_stub.cpp
   │
   └── 【目标2】ascendsk (SHARED) = src/aot/*.cpp + sk_kernel_stub.cpp  ──►  libascendsk.so
            （依赖 generate_entry_stub，确保 stub 先生成）
```

注意一个精妙的设计：设备算子二进制（`sk_entry` 的 `.o`）**不是独立下发**，而是被 `gen_sk_entry_stub.py` 转成一个 C++ 数组（`uint64_t` 数组，放进 `.sk.kernel` 段），编进 `libascendsk.so`。运行时库自带设备二进制，部署时一个 `.so` 搞定。

#### 4.3.3 源码精读

**目标 1：superkernel_whl（JIT wheel）**——用 `python3 -m build --wheel` 把整个 Python 包打成 wheel，并 install 进发布目录：

```cmake
file(GLOB_RECURSE SUPER_KERNEL_SRC CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.py")

add_custom_target(superkernel_whl ALL
    DEPENDS ${CMAKE_CURRENT_BINARY_DIR}/superkernel-0.1.0-py3-none-any.whl)
add_custom_command(
    OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/superkernel-0.1.0-py3-none-any.whl
    COMMAND ... python3 -m build --wheel --no-isolation --outdir ${CMAKE_CURRENT_BINARY_DIR} ...
    DEPENDS ${SUPER_KERNEL_SRC} pyproject.toml)
```

> 见 [super_kernel/CMakeLists.txt:L10-L25](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/CMakeLists.txt#L10-L25)。`GLOB_RECURSE *.py` 意味着任何 JIT Python 源改动都会触发 wheel 重新打包。打包细节由 `pyproject.toml` 决定，其中关键映射是：

```toml
[tool.setuptools]
package-dir = {superkernel = "src/jit/superkernel"}
```

> 见 [super_kernel/pyproject.toml:L82-L83](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/pyproject.toml#L82-L83)。这行说明 wheel 里安装出来的 `superkernel` 包，源码其实就是 `src/jit/superkernel/`——印证了 4.1 里「JIT 层 = 这个 Python 包」。

**目标 2：ascendsk（AOT 共享库）**——把 `src/aot/` 下所有 C++ 源加上生成的 stub 编成 `.so`：

```cmake
if (NOT TARGET sk_kernel)
    add_subdirectory(kernel)
endif()

aux_source_directory(${CMAKE_CURRENT_SOURCE_DIR}/src/aot SRC_FILES)

add_library(ascendsk SHARED
    ${SRC_FILES}
    ${GENERATED_SK_ENTRY_STUB_CPP})
add_dependencies(ascendsk generate_entry_stub)
```

> 见 [super_kernel/CMakeLists.txt:L33-L43](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/CMakeLists.txt#L33-L43)。注意 `${GENERATED_SK_ENTRY_STUB_CPP}` 就是 kernel 子目录生成的 stub；`add_dependencies(ascendsk generate_entry_stub)` 保证编 `ascendsk` 前 stub 一定已生成。

**kernel 子目录：按平台编 sk_entry**——循环两个平台架构，各编一个 fatbin 库，再收集它们的 `.o`：

```cmake
set(SK_KERNEL_ARCHS dav-2201 dav-3510)
foreach(SK_KERNEL_ARCH IN LISTS SK_KERNEL_ARCHS)
   string(REPLACE "-" "_" SK_KERNEL_ARCH_SUFFIX ${SK_KERNEL_ARCH})
   set(SK_ENTRY_TARGET sk_entry_${SK_KERNEL_ARCH_SUFFIX})
   ascendc_fatbin_library(${SK_ENTRY_TARGET} sk_entry.asc)
   ...
   set(SK_ENTRY_OBJECT ${CMAKE_CURRENT_BINARY_DIR}/${SK_ENTRY_TARGET}.o)
   list(APPEND SK_ENTRY_STUB_INPUTS ${SK_KERNEL_ARCH}=${SK_ENTRY_OBJECT})
endforeach()
```

> 见 [super_kernel/kernel/CMakeLists.txt:L10-L39](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/kernel/CMakeLists.txt#L10-L39)。`dav-2201` 和 `dav-3510` 是两个 NPU 架构（后者对应昇腾 950），各自编一份 `sk_entry`，运行时按设备实际 SoC 选一份加载。

**把 `.o` 二进制嵌进 C++ stub**——这是整个构建里最巧妙的一步，由脚本完成：

```cmake
add_custom_command(
   OUTPUT ${GENERATED_SK_ENTRY_STUB_CPP}
   COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/gen_sk_entry_stub.py
           ${GENERATED_SK_ENTRY_STUB_CPP} ${SK_ENTRY_STUB_INPUTS}
   ...)
```

> 见 [super_kernel/kernel/CMakeLists.txt:L41-L56](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/kernel/CMakeLists.txt#L41-L56)。

脚本把 `.o` 按 8 字节一组读成十六进制，生成一个放在 `.sk.kernel` 段的 `uint64_t` 数组：

```python
def gen_buffer_code(bin_file, symbol):
    data = get_file_content(bin_file)   # 每 8 字节 → '0x........'
    ...
    return f'''static const uint64_t {symbol}[{len(data)}] __attribute__ ((section (".sk.kernel"))) = {{
{data_lines}
}};''', len(data)
```

> 见 [super_kernel/scripts/gen_sk_entry_stub.py:L34-L47](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/scripts/gen_sk_entry_stub.py#L34-L47)。运行时 `libascendsk.so` 内的 `AscendGetEntryBinHandle()` 会根据 `aclrtGetSocName()` 判断是 `Ascend950` 还是其它，选对应架构的那段二进制加载。这样设备入口代码就和运行时库绑死成一个 `.so`，部署无需额外设备文件。

#### 4.3.4 代码实践

**实践目标**：通过构建命令验证两个产物真实存在，并理解 build.sh 如何驱动它们。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [build.sh:L545-L560](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L545-L560)，这是 `superkernel_py_ut` 函数：它先 `pip install -e .[dev]`（用 `pyproject.toml` 装出可编辑的 `superkernel` 包），再跑 pytest。这说明 **JIT 层是当作普通 Python 包来测试的**。
2. 阅读 [build.sh:L578-L604](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L578-L604)，这是 `superkernel_cpp_ut` 函数：它通过 `-DENABLE_CPP_UTEST=ON` 触发 CMake，再 `build run_super_kernel_aot_utest`。回看 `super_kernel/CMakeLists.txt` 末尾 `if(ENABLE_CPP_UTEST OR ENABLE_RDV_UTEST) add_subdirectory(tests/aot)`——这说明 **AOT 层是用 CMake + gtest 编出二进制来测的**，与 JIT 的 pytest 路径完全不同。
3. （可选，需 CANN 环境）执行 `sh build.sh --pkg -j 8`，构建成功后在 `output/` 下找到 `cann-graph-autofusion*.run`；解包后能在安装目录的库路径找到 `libascendsk.so`，在 Python 路径找到 `superkernel-0.1.0-py3-none-any.whl`。

**需要观察的现象**：

- JIT 与 AOT 的测试入口完全分离：一个走 `pytest`（Python），一个走 `cmake build run_super_kernel_aot_utest`（C++）。
- `pip install -e .[dev]` 依赖 `pyproject.toml` 的 `package-dir` 映射，把 `src/jit/superkernel` 装成 `superkernel`。

**预期结果**：

- 两个产物分别位于：`libascendsk.so`（库目录）、`superkernel-0.1.0-py3-none-any.whl`（Python 目录）。
- 若无 CANN 环境，步骤 1、2 的源码阅读结论即为「待本地验证」的运行部分。

#### 4.3.5 小练习与答案

**练习 1**：`ascendsk` 目标为什么要 `add_dependencies(ascendsk generate_entry_stub)`？去掉会怎样？

**参考答案**：因为 `ascendsk` 要编译 `${GENERATED_SK_ENTRY_STUB_CPP}` 这个生成的 `.cpp`，而它由 `generate_entry_stub` 目标产出。不加依赖，CMake 可能在 stub 生成前就开始编 `ascendsk`，导致找不到文件而失败。并行构建（`-j`）下尤其容易触发。

**练习 2**：设备二进制为什么用「嵌进 `.so`」而不是单独发布一个 `.o`/`.bin` 文件？

**参考答案**：嵌进 `.so` 后部署只需拷贝一个动态库，运行时按 SoC 名选段加载，避免设备文件丢失/路径不一致的问题；放在 `.sk.kernel` 专用段也便于运行时定位。代价是 `.so` 体积变大。

---

## 5. 综合实践

**任务**：画出 `super_kernel/` 组件的「源码分层 + 构建目标」关系图，并标注 JIT/AOT 边界与对接点。

要求在你的图里包含：

1. 四个源码目录：`src/jit/superkernel/`、`src/aot/`、`include/super_kernel/`、`kernel/`，每个用一句话注明职责。
2. 两个核心构建目标 `superkernel_whl` 与 `ascendsk`，各自产出的文件名，以及它们分别「吃」哪些源码。
3. `kernel/` 的两个平台目标（`sk_entry_dav_2201` / `sk_entry_dav_3510`）如何经 `gen_sk_entry_stub.py` 变成 `sk_kernel_stub.cpp` 并进入 `ascendsk`。
4. 用一条虚线标出 **JIT 层与 AOT 层的边界**，并写出对接的公共头文件路径。

完成后，对照本讲 4.3.2 的流程图自检：你的图是否覆盖了「设备二进制嵌进运行时库」这一关键路径？这是本讲最容易被忽略、却又最体现工程巧思的一环。

## 6. 本讲小结

- `super_kernel/` 是双层结构：`src/jit/superkernel/`（Python，编译期生成超核源码）与 `src/aot/`（C++，运行期图优化与下发），两者语言不同、时机不同。
- JIT 入口是 `super_kernel.py: compile()`，AOT 入口是 `aclskOptimize()`（实现于 `super_kernel.cpp`，内部走 `SuperKernelOptimizer::Process`）。
- `include/super_kernel/super_kernel.h` 是唯一对外公开的 C 头，定义 `aclsk*` 函数与选项结构，是跨语言/跨编译器的稳定 ABI 契约。
- 两个核心构建目标：`superkernel_whl`（→ `superkernel-0.1.0-py3-none-any.whl`，JIT 包）与 `ascendsk`（→ `libascendsk.so`，AOT 运行时）。
- `kernel/` 子目录按 `dav-2201`/`dav-3510` 两个平台编 `sk_entry`/`sk_scope`，并用 `gen_sk_entry_stub.py` 把设备 `.o` 二进制嵌进 `libascendsk.so`，实现「单 `.so` 部署」。
- JIT 与 AOT 的测试路径也完全分离：JIT 走 `pytest`，AOT 走 CMake + gtest（`run_super_kernel_aot_utest`）。

## 7. 下一步学习建议

- 下一讲 **u2-l3「JIT 代码生成入口与首个示例」** 会真正进入 `super_kernel.py: compile()` 的内部，讲清 `op_infos`/`sub_op_infos` 算子元数据，并跑通 `superkernel_scope.py` 示例——这是 JIT 层的精读起点。
- 想提前了解 AOT 运行时的读者，可以先扫一眼 `super_kernel/src/aot/sk_optimizer.cpp`、`sk_task_builder.cpp`，它们对应 u10 单元「SuperKernel 深度实现」。
- 想理解「设备二进制如何被运行时加载」的读者，可以回头细读 `gen_sk_entry_stub.py` 生成的 stub 里的 `AscendGetEntryBinHandle()` 与 `aclrtBinaryLoadFromData`，这是设备侧加载的关键。
