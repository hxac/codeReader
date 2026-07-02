# 字节码版本与兼容性

## 1. 本讲目标

本讲是「专家·字节码二进制格式」单元的第 4 篇，承接 u7-l1（翻译管线）与 u7-l2（写入器格式）。学完本讲你应当能够：

- 说清 CUDA Tile 里「字节码版本（bytecode version）」与「项目发布版本」「LLVM commit」三者为何是不同的东西。
- 掌握 `BytecodeVersion` 的三段式模型 `major.minor.tag`、它的比较运算，以及 `kCurrentVersion` / `kCurrentCompatibilityVersion` / `kMinSupportedVersion` / `kUnifiedBitfieldVersion` 四个关键常量的取值与含义。
- 理解 13.1 → 13.2 → 13.3 这条演进线分别新增了什么（f8E8M0FNU、overflow 属性、f4E2M1FN、新视图、统一 bitfield、Producer 段、全局可见性等），以及读写器如何据此做**前向（forward）**与**后向（backward）**兼容。
- 读懂 `test/Bytecode/versioning/` 下的兼容性测试用例，并能解释每条 `// CHECK` 断言背后对应的版本门机制。

本讲只讲「版本」本身；具体的字节码段格式、magic number、序列化细节已在 u7-l2 讲过，读取器的两阶段解析已在 u7-l3 讲过，这里不再重复。

## 2. 前置知识

在进入源码前，必须先分清 CUDA Tile 里三个容易混淆的「版本」概念。这一点是本讲最容易踩的坑。

### 2.1 三个不同的「版本」

CUDA Tile 同时存在三套独立的版本号，初学者常把它们混为一谈：

| 版本概念 | 形态 | 记录在哪 | 本讲是否涉及 |
| --- | --- | --- | --- |
| **项目发布版本** | `Major.Minor.Patch`（如 `13.1.5`） | git tag、README「Versioning」章节 | 否（仅背景） |
| **字节码版本** | `major.minor.tag`（如 `13.3`） | `.tilebc` 文件 header、`BytecodeVersion` 类 | **是（本讲主角）** |
| **LLVM commit** | 一个 git hash | `cmake/IncludeLLVM.cmake` | 否（u2-l1 已讲） |

- **项目发布版本**：跟随 CUDA Toolkit 的 `Major.Minor`，`Patch` 单独统计开源发布次数（如 `13.1.5` 表示与 CUDA Toolkit 13.1.x 兼容的第 6 次开源发布）。详见 [README.md:342-353](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L342-L353)。
- **字节码版本**：二进制格式 `.tilebc` 自己的版本，写进文件头三个字节里，是**编译器/驱动之间互操作的契约**。本讲全文都在讨论它。
- **LLVM commit**：项目寄生在 MLIR 上、锁定到具体 commit（u2-l1），与字节码版本无关。

> 一句话记忆：**项目版本说「这是哪一版 CUDA Tile」，字节码版本说「这个 `.tilebc` 文件用哪种二进制格式」，LLVM commit 说「拿哪一份上游代码编译」**。

### 2.2 为什么要单独搞一套字节码版本

`.tilebc` 是一个会被**持久化、跨工具传递**的二进制文件：`cuda-tile-translate` 写出它，`tileiras`（AoT）或 CUDA 驱动（JIT）消费它。消费方的驱动/工具版本可能比产出方**旧**（旧的驱动读新的字节码），也可能**新**（新的工具读旧的存量字节码）。因此字节码格式必须有一套明确的版本约定，回答两个问题：

1. **写入端**：当我用 `--bytecode-version=13.1` 显式指定一个旧格式时，源程序里用到的「13.3 才有的特性」该怎么办？（答：报错或静默降级。）
2. **读取端**：当一个文件头声明自己是 13.1，但内部却出现了 13.3 才有的类型/操作时，该如何处置？（答：拒绝并报错。）

这套约定就是本讲的「兼容性策略」。前置术语：序列化/反序列化（u7-l1）、section/magic/header（u7-l2）、`fromVersion`/解析两阶段（u7-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Bytecode/Common/Version.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h) | `BytecodeVersion` 类声明、四个版本常量、`getSupportedVersions` 声明 |
| [lib/Bytecode/Common/Version.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp) | 四个常量的具体取值（13.1/13.3）、`isOpcodeAvailableInVersion` 实现、`getSupportedVersions` 由 tblgen 生成 |
| [include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td) | 声明支持的版本（`SupportedVersion`）、为每个操作**冻结**分配 opcode |
| [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | `CudaTileOpDef` 上的 `operationVersion` / `sinceVersion` 字段——版本信息的源头 |
| [tools/cuda-tile-tblgen/BytecodeGen.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp) | 由 `.td` 生成「版本→最大 opcode 映射」与各属性/操作数/结果的版本门代码 |
| [test/Bytecode/versioning/](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning) | 前向/后向兼容的 lit/FileCheck 测试集 |

## 4. 核心概念与源码讲解

### 4.1 BytecodeVersion：三段式版本模型与比较运算

#### 4.1.1 概念说明

字节码版本用三个数刻画：`verMajor.verMinor.verTag`。其中 `major.minor` 对齐 CUDA Toolkit 的大版本节奏（当前都是 13.x），`tag` 是格式内部的细分修订（目前公开版本均为 0，测试里会出现非 0 的 tag）。它是一个**值类型**：默认构造、可比较、可转成字符串，整个类没有虚函数、没有运行时状态。

之所以做成独立的值类型而不是直接用 `uint32_t`，是为了把「版本是否合法」「两个版本谁更新」「打印成字符串」这些行为集中封装，避免散落在读写器里的裸整数比较。

#### 4.1.2 核心流程

版本的**比较是字典序（lexicographic）**：先比 major，再比 minor，最后比 tag。用数学语言写就是，对两个版本 \(v=(M,m,t)\) 与 \(v'=(M',m',t')\)：

\[
v < v' \iff (M<M')\;\lor\;\bigl(M=M'\land m<m'\bigr)\;\lor\;\bigl(M=M'\land m=m'\land t<t'\bigr)
\]

`fromVersion` 工厂方法把 `(major, minor, tag)` 三元组转成 `BytecodeVersion`，若三元组不是表里登记过的合法版本则返回 `std::nullopt`——合法性由 tblgen 生成的校验代码判定（见 4.2.3）。

#### 4.1.3 源码精读

`BytecodeVersion` 的比较运算集中在 [Version.h:44-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h#L44-L64)，正是上面字典序公式的直译：

```cpp
bool operator<(const BytecodeVersion &other) const {
  if (verMajor != other.verMajor)
    return verMajor < other.verMajor;
  if (verMinor != other.verMinor)
    return verMinor < other.verMinor;
  return verTag < other.verTag;
}
```

其余 `<=`/`>`/`>=` 都由 `<` 与 `==` 复合得出（[Version.h:58-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h#L58-L64)），是典型的「只定义一个全序基准，其余派生」写法。

`toString`（[Version.h:67-71](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h#L67-L71)）在 `tag==0` 时只打印 `major.minor`，否则打印 `major.minor.tag`——这就是为什么报错信息里你看到的是 `13.1` 而不是 `13.1.0`。

`fromVersion` 的本体在 [Version.cpp:25-31](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L25-L31)，它只 `#include "StaticOpcodes.inc"` 里 tblgen 生成的 `GEN_VERSION_VALIDATION` 片段——合法性判定本身也是「单一数据源」由 `.td` 驱动的。

#### 4.1.4 代码实践

1. **目标**：直观感受版本比较与 `--list-versions`。
2. **步骤**：构建完成后运行 `cuda-tile-translate --list-versions`。
3. **观察**：终端会逐行打印当前构建支持的全部字节码版本。
4. **预期结果**：正常构建会打印 `13.1`、`13.2`、`13.3` 三行（在开启了测试、定义了 `TILE_IR_INCLUDE_TESTS` 的构建里，还会额外出现 `250.0`、`250.1` 两个仅供测试用的版本，见 [BytecodeOpcodes.td:60-62](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L60-L62)）。若手头没有可运行的二进制，则**待本地验证**。
5. **延伸阅读**：`--list-versions` 的实现见 [CommandLineOptions.cpp:111-123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L111-L123)，它在解析期就调用 `getSupportedVersions()` 打印并 `exit(0)`，所以即使后续命令行有错也不会执行到。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `BytecodeVersion` 只定义了 `operator<` 与 `operator==`，却没有单独定义 `operator>` 的完整逻辑？
  - **答案**：因为版本是一个全序集，`>` 可由 `other < *this` 直接得到（见 [Version.h:61](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h#L61)）。只维护一个基准比较可避免逻辑漂移。
- **练习 2**：`fromVersion(13, 1, 5)` 与 `fromVersion(13, 1, 0)` 在 13.1 这个版本上分别返回什么？
  - **答案**：`fromVersion(13,1,0)` 返回合法的 13.1；而 tag=5 不是表里登记过的合法 tag，`fromVersion(13,1,5)` 会返回 `std::nullopt`（具体取决于 tblgen 生成的合法集合，待本地验证）。

### 4.2 四个关键版本常量与 getSupportedVersions

#### 4.2.1 概念说明

`BytecodeVersion` 类暴露四个静态常量，它们定义了「格式现在到哪了」「为了最广兼容该写哪个」「最低能读多老」「格式何时改了编码细节」这四个边界。理解它们的取值差异，是理解兼容性策略的前提。

- **`kCurrentVersion`**：格式当前的最新版本（本构建是 `13.3`）。`--bytecode-version` 不指定时的默认值。
- **`kCurrentCompatibilityVersion`**：当前主版本下**兼容范围最广**的版本（本构建是 `13.1`），「一般对应上一个主版本的最后一个 minor」。`BytecodeVersion` 默认构造就用它。
- **`kMinSupportedVersion`**：读取器愿意接受的**最低**版本（也是 `13.1`）。比它还老的文件直接拒绝。
- **`kUnifiedBitfieldVersion`**：可选参数（OptionalEnum/OptionalType 等）编码方式从「内联 flag」改为「统一 bitfield」的分界版本（`13.3`）。

#### 4.2.2 核心流程

四个常量把版本轴切成几段，读写器据此分流：

```
        kMinSupportedVersion           kCurrentCompatibilityVersion        kCurrentVersion / kUnifiedBitfieldVersion
            13.1                              13.1                                13.3
  <-----------|----------------------------------|-----------------------------------|------------>
     拒绝读取      可读范围 [13.1, 13.3]                                                最新可写
```

- 读取端：文件版本 `< kMinSupportedVersion` 或 `> kCurrentVersion` 一律拒绝（见 4.4）。
- 写入端：目标版本 `< kMinSupportedVersion` 拒绝；不指定时默认 `kCurrentVersion`。
- 想要「最广兼容」的产物，应**显式**指定 `--bytecode-version=13.1`（即 `kCurrentCompatibilityVersion`）。README 的端到端示例就是这么做的：[README.md:328](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L328)。

`getSupportedVersions()` 返回这张表里**全部已登记的合法版本**，是命令行 `--bytecode-version` 合法取值的来源，也是 `--list-versions` 的数据源。

#### 4.2.3 源码精读

四个常量的取值定义在 [Version.cpp:39-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L64)：

```cpp
const BytecodeVersion BytecodeVersion::kCurrentCompatibilityVersion = {/*Major=*/13, /*Minor=*/1, /*Tag=*/0};
const BytecodeVersion BytecodeVersion::kCurrentVersion             = {/*Major=*/13, /*Minor=*/3, /*Tag=*/0};
const BytecodeVersion BytecodeVersion::kUnifiedBitfieldVersion     = {/*Major=*/13, /*Minor=*/3, /*Tag=*/0};
const BytecodeVersion BytecodeVersion::kMinSupportedVersion        = {/*Major=*/13, /*Minor=*/1, /*Tag=*/0};
```

注意 `kCurrentCompatibilityVersion` 与 `kMinSupportedVersion` 当前恰好都是 13.1——前者是「为了最广兼容主动选择写的版本」，后者是「读取端能接受的最低版本」，语义不同，数值相同只是因为 13.1 既是当前主版本下兼容最广的、又是格式有据可查的最老的。

`kUnifiedBitfieldVersion` 的语义见头文件注释 [Version.h:87-90](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/Version.h#L87-L90)：13.3 起所有可选参数（Type/Enum 等）统一用一个 bitfield 编码，13.3 之前 `OptionalEnum` 走内联 flag。这意味着读写器在解析某条指令的「可选参数位」时，要**按文件头声明的版本**选两种解码路径之一。

合法版本表本身不在 C++ 里手写，而在 `.td` 里声明，由 tblgen 生成 `getSupportedVersions()`。声明见 [BytecodeOpcodes.td:55-57](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L55-L57)：

```
def : SupportedVersion<13, 1>;
def : SupportedVersion<13, 2>;
def : SupportedVersion<13, 3>;
```

`getSupportedVersions()` 的实现体是 [Version.cpp:83-85](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L83-L85) 里展开的 `GEN_SUPPORTED_VERSIONS_LIST` 宏——再次体现「`.td` 是唯一数据源」。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把「四个常量各是什么」与「`.td` 里登记了哪些版本」对齐。
2. **步骤**：打开 [Version.cpp:39-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L64)，把四个常量抄成一张表；再打开 [BytecodeOpcodes.td:55-63](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L55-L63)，数清楚有几条 `SupportedVersion`。
3. **观察**：注意 250.0 / 250.1 被 `#ifdef TILE_IR_INCLUDE_TESTS` 包住（[BytecodeOpcodes.td:60-62](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L60-L62)）。
4. **预期结果**：你能解释「为什么正式发布版只接受 13.1/13.2/13.3，而开发者本地构建的测试版还能接受 250.x」。这是 4.4 演进测试里大量使用 `250.0`/`250.1` 的原因。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 README 的 AoT 示例要写 `--bytecode-version=13.1` 而不是省略它？
  - **答案**：省略时默认取 `kCurrentVersion=13.3`（[CommandLineOptions.cpp:79](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L79)）。为了让旧一些的驱动/工具也能加载产物，应主动降到兼容性版本 13.1。
- **练习 2**：`kCurrentCompatibilityVersion` 与 `kMinSupportedVersion` 数值都是 13.1，能否合并成一个常量？
  - **答案**：语义上不应合并。前者描述「写入端的最优兼容目标」，后者描述「读取端的最老接受线」。未来如果格式演进，二者会分开（例如兼容版本可能停在 13.1 而最低支持版本被抬高，反之亦然）。分开命名让意图自解释。

### 4.3 opcode 冻结与「版本→最大 opcode」映射

#### 4.3.1 概念说明

字节码里每条指令开头都有一个 opcode（变长整数），用来标识「这是什么操作」。为了**向后兼容**（新的读取器永远要能读旧的文件），opcode 一旦分配就**永不重新编号**——这条铁律写在 [BytecodeOpcodes.td:65-67](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L65-L67) 的注释里：「must never be renumbered for backward compatibility」。

于是问题来了：当某个操作是「13.3 才新增的」（如 `alloca`、`mmaf_scaled`），而用户却要求 `--bytecode-version=13.1` 时，读取器和写入器怎么知道这个 opcode 在 13.1 里还不存在？

答案是一个**两段式**设计：
1. **粗粒度门（opcode 级）**：用「版本 → 该版本下可用 opcode 的最大值」映射，做一个 O(1) 的整数比较。
2. **细粒度门（特性级）**：针对同一操作内部「新增的属性/操作数/结果/类型」，用各自的 `sinceVersion` 单独判定（见 4.4）。

本节先讲第一道门。

#### 4.3.2 核心流程

`isOpcodeAvailableInVersion(opcode, version)` 的判定逻辑极其简单（[Version.cpp:70-77](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L70-L77)）：

```
查表 map[(version.major, version.minor)] -> 该版本最大 opcode maxOp
若版本不在表里            -> 不可用
否则 opcode <= maxOp       -> 可用
否则                       -> 不可用
```

而这张 `map` 是 `cuda-tile-tblgen` 在构建期算出来的（[BytecodeGen.cpp:743-824](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L743-L824)），算法是：

1. 遍历所有 `PublicOpcode`，取每个操作自身的 `operationVersion`（来自 [Dialect.td:88](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L88)），把它的 opcode 记到「该版本的最大 opcode」桶里取 max。
2. 把所有已知版本排序，做一次**前向填充**：若某版本没有引入任何新操作（其桶为 0），就继承上一版本的最大值（[BytecodeGen.cpp:796-802](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L796-L802)）。

> 设计要点：新增操作总是**追加在 opcode 序列末尾**（13.3 的新操作 `pack/unpack/alloca/mmaf_scaled/make_gather_scatter_view/make_strided_view/atomic_red_view_tko` 占用 `0x6F`–`0x75`，见 [BytecodeOpcodes.td:164-170](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L164-L170)）。这样「全新操作」会拿到一个比旧版本最大 opcode 还大的编号，整数比较就能直接判出「13.1 里还没有它」。

#### 4.3.3 源码精读

`isOpcodeAvailableInVersion` 本体（[Version.cpp:70-77](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L70-L77)）：

```cpp
bool isOpcodeAvailableInVersion(uint32_t opcode, const BytecodeVersion &version) {
  auto it = getVersionToMaxOpcodeMap().find({version.getMajor(), version.getMinor()});
  if (it == getVersionToMaxOpcodeMap().end())
    return false;
  return opcode <= it->second;
}
```

它在读写两端各被调用一次：

- **写入端**：`FunctionTableWriter::writeOperation` 在写 opcode 前先校验（[BytecodeWriter.cpp:960-966](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L960-L966)），不通过则报 `operation '...' is not available in bytecode version 13.1`。
- **读取端**：`InstructionParser::parseOperation` 在分派前先校验（[BytecodeReader.cpp:1862-1868](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1862-L1868)），不通过则报 `unsupported opcode N for bytecode version 13.1`。

这两处对偶调用，保证了「写不出 13.1 不支持的操作」与「读不进 13.1 不支持的操作」双向闭合。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：追踪一个 13.3 新操作在「目标 13.1」时的拒绝路径。
2. **步骤**：以 `alloca`（opcode `0x71`，[BytecodeOpcodes.td:166](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L166)）为例。设 13.1 的最大 opcode 为 `M₁`（由 13.1 操作集合取 max 得到）。追问：`0x71 <= M₁` 成立吗？
3. **观察**：因为 13.3 新操作都追加在末尾、编号更高，`M₁` 必然 `< 0x71`，所以 `isOpcodeAvailableInVersion(0x71, 13.1)` 返回 `false`。
4. **预期结果**：写出端命中 [BytecodeWriter.cpp:964-966](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L964-L966) 的报错分支；读取端命中 [BytecodeReader.cpp:1866-1868](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1866-L1868)。可用 `test/Bytecode/versioning/test_version_errors.mlir` 里 `CHECK-OP-NOT-AVAILABLE` 那条用例验证（见 4.4.3）。
5. **注意**：这是**粗粒度**门，只对「整条新操作」有效；同一个操作「新增某个属性」不靠它，靠 4.4 的细粒度门。

#### 4.3.5 小练习与答案

- **练习 1**：如果有人把一个已分配的 opcode 重新分配给另一个操作，会破坏什么？
  - **答案**：破坏向后兼容——旧的 `.tilebc` 文件里那个 opcode 会被新读取器解释成另一个操作，导致静默错误。这正是「opcode FROZEN、永不重新编号」铁律存在的原因。
- **练习 2**：前向填充（`prevMaxOpcode`）解决什么问题？
  - **答案**：若某个版本（如假设的某 13.2）没有引入任何全新操作，则它在该映射里没有自己的桶；前向填充让它继承前一版本的最大 opcode，从而 `map[13.2]` 仍是一个合法可用值，避免读取 13.2 文件时误判所有操作都「不可用」（见 [BytecodeGen.cpp:796-802](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L796-L802)）。

### 4.4 sinceVersion 驱动的细粒度前后向兼容

#### 4.4.1 概念说明

粗粒度门只能判「整条操作是否存在」。但真实的格式演进大多是「老操作长出了新东西」：

- 13.2：整数运算的 `overflow` 属性、`print` 改名为 `print_tko` 并多出一个 `token` 结果、新增 `f8E8M0FNU` 类型。
- 13.3：新增 `f4E2M1FN` 等低精度类型、新增若干视图类型、可选参数改为统一 bitfield、新增 Producer 段、global 增加可见性属性、新增 `pack/unpack/alloca/mmaf_scaled` 等操作。

这些「长出新东西」的情况，靠 opcode 整数比较是判不出来的（操作本身早就存在）。于是 CUDA Tile 给操作、属性、操作数、结果、类型各自挂了一个 `sinceVersion` 标注，由 `cuda-tile-tblgen` 为每个标注生成一段「版本门」代码，挂在写入器与读取器里。这套机制同时支撑两种兼容方向：

- **前向兼容（forward）**：旧版本的写入器/读取器遇到「自己不认识的新特性」时，要么报错、要么**静默忽略/降级**，使旧工具不被新格式噎死。
- **后向兼容（backward）**：新版本的读取器能正确读懂旧格式文件，必要时把旧形态「升级」成新 IR（如把 13.1 的 `print` 读成 13.2 的 `print_tko`）。

#### 4.4.2 核心流程

`sinceVersion` 的源头在 `.td`：操作的基类 `CudaTileOpDef` 把版本存进 `operationVersion` 与 `metadata.sinceVersion`（[Dialect.td:79-97](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L79-L97)）；属性、枚举、类型同理各带 `sinceVersion`。tblgen 据此生成两类产物：

1. **写入端版本门**（`BytecodeGen.cpp` / `BytecodeTypeCodeGen.cpp`）：序列化某属性/操作数/结果/类型前，先比较 `config.bytecodeVersion` 与该项的 `sinceVersion`：
   - 目标版本 **≥** sinceVersion → 正常写出。
   - 目标版本 **<** sinceVersion：
     - 必填属性：若取值等于默认值 → 静默不写（旧版本读到默认值即可）；若取值非默认 → **报错**。
     - 可选属性/操作数：提供了 → 报错；未提供 → 静默跳过。
     - 结果/类型：被使用 → 报错；否则不序列化。
2. **读取端版本门**：按文件头声明的版本，决定要不要去解析某段「可选参数」、要不要把旧形态升级（如 `print`→`print_tko`、统一 bitfield vs 内联 flag）。

报错信息的措辞在生成器里硬编码，因此 13.1/13.2/13.3 与测试用的 250.x 都共用同一套话术，例如必填属性报 `attribute '<name>' requires bytecode version <V>+, but targeting <T>`（[BytecodeGen.cpp:468](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L468)）。

读取端的「最低/最高版本」门在 `parseHeader` 里：文件版本不在 `[kMinSupportedVersion, kCurrentVersion]` 区间就整体拒绝（[BytecodeReader.cpp:317-326](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L317-L326)）；写入端的对应门在 [BytecodeWriter.cpp:175-181](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp#L175-L181)。

#### 4.4.3 源码精读（以测试为线索）

理解这套机制最快的方式是读 `test/Bytecode/versioning/`。这里按「写入端报错 / 读取端报错 / 静默降级 / 后向升级」四类各举一例。

**(a) 写入端：用 13.2 特性却目标 13.1 → 报错。** [oldVersionRejectionTest.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/oldVersionRejectionTest.mlir) 给 `negi` 带上 `overflow<no_signed_wrap>`（13.2 才有的属性），却指定 `-bytecode-version=13.1`：

```
// CHECK: attribute 'overflow' requires bytecode version 13.2+
```

这正对应必填属性非默认值的报错分支（[BytecodeGen.cpp:464-472](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L464-L472)）。

**(b) 写入端：用 13.3 新类型却目标 13.1 → 报错。** [new_types.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/new_types.mlir) 用 `tile<f8E8M0FNU>`（13.2 类型）目标 13.1：

```
// expected-error {{type 'Float8E8M0FNU' requires bytecode version 13.2+, targeting 13.1}}
```

这条话术由 `BytecodeTypeCodeGen.cpp` 生成（写入端类型版本门）。

**(c) 读取端：文件头说 13.1，内容却有 13.3 类型 → 拒绝。** [new_types_reader_error.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/new_types_reader_error.mlir) 喂给读取器一个「声称 13.1、内含 `f4E2M1FN`（13.3 类型）」的预制坏文件：

```
// CHECK: type 'Float4E2M1FN' requires bytecode version 13.3+, file version is 13.1
```

注意读取端话术是 `file version is <T>`，与写入端的 `targeting <T>` 区分开，便于从报错判断是「写错了目标版本」还是「文件本身造假」。

**(d) 静默降级：13.3 的 Producer 段目标 13.1 → 静默丢弃。** [producer_attr_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/producer_attr_backward_compat.mlir) 给模块挂 `producer = "..."`（13.3 才有序列化的 Producer 段），目标 13.1 写出再读回：

```
// CHECK-NOT: producer
```

即「旧目标写不出 Producer 段、读回时也没有」，**不报错**——因为 producer 是可降级的附加信息，丢掉不影响语义。

**(e) 后向升级：13.1 的 `print`（0 结果）→ 读成 13.2 的 `print_tko`（1 个 token 结果）。** [print_tko_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/print_tko_backward_compat.mlir) 用一个 13.1 的存量字节码，断言新读取器把它还原成带 token 结果的 `print_tko`，且不破坏后续 SSA 编号：

```
// CHECK: print_tko "Iteration result" -> token
// CHECK: mmaf %{{.*}}, %{{.*}}, %{{.*}} : tile<256x256xf64>, ...
```

[versioned_results_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/versioned_results_backward_compat.mlir) 进一步验证：目标 13.1 时 `print_tko` 的 token 结果**不序列化**，但 SSA 编号仍正确（后续 `atomic_rmw_tko`、第二个 `print_tko` 都能对齐）。

**(f) 演进测试：用 250.x 模拟「未来版本」。** 因为正式版本只有 13.1/13.2/13.3，难以演示「操作逐版本长出新属性/操作数/结果」，项目用只在测试构建里启用的 250.0/250.1 配合专用测试操作 `testing$bytecode_test_evolution` / `testing$bytecode_test_new_attribute` 来演练。[test_version_errors.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/test_version_errors.mlir) 一次覆盖四种写入端报错（属性 / 可选属性 / 可选操作数 / 结果）与一种 opcode 不可用；[test_forward_compatibility.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/test_forward_compatibility.mlir) 则验证「不用新特性时，250.0 与 250.1 都能 round-trip」——即前向兼容的正确一面。

#### 4.4.4 代码实践

1. **目标**：亲手触发一次「13.3 类型 vs 13.1 目标」的写入端报错，再对照读取端坏文件用例。
2. **步骤**：
   - 准备 `f4.mlir`，内含一个使用 13.3 才有的 `f4E2M1FN` 类型的最小内核。
   - 运行 `cuda-tile-translate -mlir-to-cudatilebc -no-implicit-module --bytecode-version=13.1 f4.mlir -o /dev/null`。
   - 再运行 `--bytecode-version=13.3` 同样的命令。
3. **观察**：13.1 目标会失败并打印类型版本错误；13.3 目标会成功生成 `.tilebc`。
4. **预期结果**：13.1 报错形如 `type 'Float4E2M1FN' requires bytecode version 13.3+, targeting 13.1`（话术同 [new_types.mlir:3](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/new_types.mlir#L3)，把 `Float8E8M0FNU/13.2` 换成 `Float4E2M1FN/13.3`）。若手头没有可运行二进制，则**待本地验证**。
5. **延伸**：把 `f4.mlir` 改成用 `f8E8M0FNU`（13.2 类型），分别目标 13.1 与 13.2，复现 [new_types.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/new_types.mlir) 的断言。

#### 4.4.5 小练习与答案

- **练习 1**：写入端对「必填属性在旧目标下取了非默认值」报错，但对「可选属性未提供」却静默跳过。为什么策略不同？
  - **答案**：必填属性取非默认值意味着 IR 真的携带了旧格式无法表达的语义，写出去会失真，必须报错（[BytecodeGen.cpp:464-472](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L464-L472)）；而可选属性「未提供」本就等价于「旧格式的默认行为」，跳过正好等价，无需报错。
- **练习 2**：`producer` 在目标 13.1 时被丢弃却不报错（[producer_attr_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/producer_attr_backward_compat.mlir)），而 `overflow` 在目标 13.1 时取非默认值就报错。区别在哪？
  - **答案**：`producer` 是不影响语义的元信息（丢了也能正确执行内核），属于可降级；`overflow` 是影响整数运算语义的提示（丢失会改变未定义行为的判定边界），属于不可降级。可降级特性静默丢弃，不可降级特性必须报错。

## 5. 综合实践

把本讲四个模块串起来，做一次「版本演进全链路」调查。

**任务**：用一张表把「13.1 / 13.2 / 13.3 各新增了什么、由哪条版本门保证旧工具不被噎到」整理清楚，并用测试用例佐证。

**操作步骤**：

1. **类型维度**：用一段使用 `f4E2M1FN` 的 MLIR，分别以 `--bytecode-version=13.1` 与 `13.3` 写出（见 4.4.4）。记录 13.1 的报错全文与 13.3 的成功结果。
2. **属性维度**：阅读 [oldVersionRejectionTest.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/oldVersionRejectionTest.mlir)，解释为何 `overflow<no_signed_wrap>` 不能写进 13.1。
3. **操作维度**：阅读 [test_version_errors.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/test_version_errors.mlir) 的 `CHECK-OP-NOT-AVAILABLE`，把它对应到 [BytecodeReader.cpp:1862-1868](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp#L1862-L1868) 的粗粒度 opcode 门。
4. **后向兼容维度**：阅读 [print_tko_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/print_tko_backward_compat.mlir)，解释「结果个数随版本变化」如何不破坏 SSA 编号（提示：结合 [versioned_results_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/versioned_results_backward_compat.mlir)）。
5. **静默降级维度**：阅读 [producer_attr_backward_compat.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/producer_attr_backward_compat.mlir)，区分「可降级」与「不可降级」特性。

**预期产出**：一张三列的表——`版本 | 新增内容（类型/属性/操作/段） | 兼容策略与对应测试`。例如：

| 版本 | 新增内容 | 兼容策略 | 佐证测试 |
| --- | --- | --- | --- |
| 13.2 | `f8E8M0FNU` 类型 | 旧目标写/读均报错 | new_types.mlir / new_types_reader_error.mlir |
| 13.2 | `overflow` 属性 | 取非默认值目标 13.1 报错 | oldVersionRejectionTest.mlir |
| 13.2 | `print`→`print_tko` 多 token 结果 | 新读取器后向升级旧文件 | print_tko_backward_compat.mlir |
| 13.3 | `f4E2M1FN` 等新类型 | 旧目标报错 | 4.4.4 实践 |
| 13.3 | Producer 段 | 旧目标静默丢弃 | producer_attr_backward_compat.mlir |
| 13.3 | 统一 bitfield 编码 | 读取端按文件版本选解码路径 | kUnifiedBitfieldVersion 注释 |

若无法实际构建运行，请把这张表填满并对每行写一句话解释，标注「待本地验证」。

## 6. 本讲小结

- CUDA Tile 里有三套独立的「版本」：项目发布版本（`Major.Minor.Patch`）、**字节码版本（`major.minor.tag`，本讲主角）**、LLVM commit，不可混为一谈。
- `BytecodeVersion` 是字典序全序的值类型；四个关键常量分别是 `kCurrentVersion=13.3`、`kCurrentCompatibilityVersion=13.1`、`kMinSupportedVersion=13.1`、`kUnifiedBitfieldVersion=13.3`，分别定义「最新可写」「最广兼容目标」「最老可读」「编码方式分界」。
- opcode 一旦分配**永不重新编号**；`isOpcodeAvailableInVersion` 用 tblgen 生成的「版本→最大 opcode」映射做 O(1) 粗粒度门，主要拦截「整条新操作」（它们追加在 opcode 末尾）。
- 同一操作「长出新属性/操作数/结果/类型」由各处 `sinceVersion` 标注驱动细粒度版本门，报错话术由 `BytecodeGen.cpp`/`BytecodeTypeCodeGen.cpp` 统一生成。
- 兼容是双向的：**前向**让旧工具遇新特性时报错或静默降级（如 Producer 段丢弃），**后向**让新读取器能升级旧形态（如 `print`→`print_tko` 且不破坏 SSA 编号）。
- `test/Bytecode/versioning/` 是这套机制的「规格说明书」，用 250.x 测试版本演练了「逐版本长出新特性」的全部分支。

## 7. 下一步学习建议

- **回到代码生成层**：本讲频繁提到「tblgen 由 `.td` 生成版本门代码」。下一站建议读 u8-l1（cuda-tile-tblgen 字节码代码生成），看 [BytecodeGen.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp) 与 `BytecodeReaderGen.cpp` 如何把 `sinceVersion` 一一翻译成 `Bytecode.inc`/`BytecodeReader.inc` 里的 C++ 片段。
- **看一整条「新增操作」要改哪些地方**：结合本讲的 opcode 冻结规则与 `operationVersion` 字段，尝试列出「给方言加一个 13.4 新操作」需要触碰的全部文件（`.td` 声明、`BytecodeOpcodes.td` 分配 opcode、`SupportedVersion` 登记、读写两端代码生成）。
- **优化器如何对待版本**：u9 系列会讲 `cuda-tile-optimize`；可留意优化前后字节码版本是否保持，以及 `SynthesizeDebugInfoScopes` 等 pass 产出的 DI 信息在不同目标版本下的写入行为。
