# 目录结构与源码组织

## 1. 本讲目标

本讲承接上一讲「项目定位与工具全景」。上一讲我们知道了 asc-tools 有五个工具、各自解决什么问题；本讲要回答的是：**这些工具的代码到底放在仓库的哪里？它们是怎么被组织成一个可编译的整体的？**

读完本讲，你应当能够：

1. 看懂 asc-tools 仓库的**顶层目录布局**，知道每个目录存放什么职责的文件。
2. 理解 **C++ 工具（cpudebug）与 Python 工具（npuchk / msobjdump / show_kernel_debug_data / optype_collector）在目录上是如何分离**的，以及为什么这样分。
3. 在源码中**快速定位任意一个工具的主入口文件**，拿到一把「在庞大仓库里找路」的钥匙。

本讲只读目录与构建文件，不进入任何工具的内部实现，不需要你写过 Ascend C 代码。

---

## 2. 前置知识

上一讲已经介绍了 CANN、NPU、Ascend C、Kernel、CPU 域 / NPU 域等术语，这里不再重复。本讲额外用到两个概念：

- **C++ 工具 vs Python 工具**：cpudebug 是用 C++ 写的「调试库」，它最终会被编译成 `.so` 共享库，链接进你的算子二进制；而 npuchk、msobjdump、show_kernel_debug_data、optype_collector 是用 Python 写的「脚本工具」，直接由 Python 解释器运行。这两类工具的代码组织方式完全不同，所以仓库把它们分开放。
- **CMake**：一个跨平台的 C/C++ 构建工具。你可以把它理解成「C++ 版的 Make」，通过 `CMakeLists.txt` 文件描述「要编译什么、怎么编译、装到哪里」。asc-tools 用 CMake 把 C++ 部分和 Python 部分统一管理起来。
- **`add_subdirectory`**：CMake 的一条指令，意思是「进入这个子目录，继续读取它里面的 `CMakeLists.txt`」。它构成了仓库里模块之间的「组装关系」。
- **`__main__.py`**：Python 的一个约定文件。当你用 `python -m 模块名` 运行一个包时，Python 会自动执行这个包里的 `__main__.py`，所以它通常是一个工具的**命令行入口**。

> 提示：如果你对 CMake 完全陌生，本讲只需要理解「`CMakeLists.txt` 描述构建规则、`add_subdirectory` 把子模块挂进来」即可，不必深究语法。

---

## 3. 本讲源码地图

本讲是「地图篇」，主要看仓库的骨架文件，涉及的关键文件如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目说明，其中 `目录结构说明` 一节给出官方的目录树，是本讲的主线。 |
| `CMakeLists.txt` | 仓库根构建文件，用一连串 `add_subdirectory` 把各个工具模块挂进构建系统。 |
| `cpudebug/CMakeLists.txt` | C++ 核心（cpu debug）的构建文件，展示了多架构编译与源码子目录组织。 |
| `npuchk/CMakeLists.txt` | npu check 工具的构建/安装文件。 |
| `tests/CMakeLists.txt` | 测试构建文件，展示 C++ UT 与 Python UT 如何分发。 |

> 说明：永久链接基准地址为
> `https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/`
> 本讲所有源码引用都基于该 HEAD 提交。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**目录结构说明**、**CMake 子目录组织**、**工具源码定位**。

### 4.1 顶层目录结构说明

#### 4.1.1 概念说明

一个健康的项目仓库，目录划分通常遵循「**按职责分目录**」的原则：构建脚本放一起、源码放一起、文档放一起、测试放一起、样例放一起。asc-tools 也是如此。

在动手翻目录之前，先记住一个关键认知：**asc-tools 是「一个 C++ 核心 + 四个围绕其产物的 Python 工具」的组合体**。

- **C++ 核心**：`cpudebug/`，提供 CPU 域孪生调试的底层能力，体量最大、最复杂。
- **Python 工具**：分散在 `npuchk/` 和 `utils/` 下，体量小、各自独立。

把握住这条主线，下面的目录树就不会乱。

#### 4.1.2 核心流程

我们先按「职能」把顶层目录分成五组来看：

| 分组 | 目录 | 职责 |
| --- | --- | --- |
| 源码 | `cpudebug/` | cpu debug 的 C++ 实现源代码（核心） |
| 源码 | `npuchk/` | npu check 的 Python 解析脚本 |
| 源码 | `utils/` | 其余三个 Python 工具（msobjdump / show_kernel_debug_data / optype_collector）+ 工程模板 templates |
| 构建 | `cmake/`、根 `CMakeLists.txt`、`build.sh` | 构建脚本与 CMake 模块 |
| 辅助 | `docs/`、`examples/`、`tests/`、`libraries/`、`third_party/`、`scripts/` | 文档、样例、测试、依赖库、打包脚本 |

这样分组后，仓库的全景就很清晰了：**「源码」目录产出工具，「构建」目录把工具编出来，「辅助」目录提供文档、样例和测试**。

#### 4.1.3 源码精读

README 中专门有一节 `目录结构说明`，给出了官方目录树：

参见 [README.md:L24-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L24-L45)。这段目录树把每个顶层目录的职责都用注释标了出来，比如：

- `cpudebug`：cpu debug 工具实现源代码，内部又细分为 `cmake / include / utils / src`。
- `npuchk`：npu check 检查工具。
- `utils`：存放 `msobjdump` 和 `show_kernel_debug_data` 的实现源代码。
- `docs / examples / libraries / scripts / tests / third_party / cmake`：分别对应文档、样例、依赖库、打包脚本、UT 用例、第三方库、构建源代码。

> ⚠️ 一个值得注意的细节：README 的目录树**并不完整**。它只列出了 `utils/` 下的 `msobjdump` 和 `show_kernel_debug_data`，但仓库里 `utils/` 实际还包含 `optype_collector/`（算子信息采集工具）和 `templates/`（算子工程模板）两个目录。
>
> 这提醒我们：**README 是入门地图，但不是精确清单**。要看仓库的真实结构，最可靠的方式是直接列出目录或阅读根 `CMakeLists.txt`（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：把 README 的「文字目录树」和仓库的「真实目录」对照一遍，建立立体的空间感。

**操作步骤**：

1. 打开 [README.md:L24-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L24-L45)，阅读目录树。
2. 在本地仓库根目录执行 `ls`，把真实顶层目录和 README 的树逐一对照。
3. 重点确认 `utils/` 下真实有哪些子目录（预期：`msobjdump / show_kernel_debug_data / optype_collector / templates`）。

**需要观察的现象**：README 树里 `utils/` 只画了两个子目录，而本地 `ls utils/` 会出现四个。

**预期结果**：你会直观感受到「文档简化 vs 源码真实」的差异，并记住 `optype_collector` 和 `templates` 也位于 `utils/` 下。

#### 4.1.5 小练习与答案

**练习 1**：根据本节的分组，`libraries/` 和 `third_party/` 都和「依赖」有关，它们有什么不同？

> **参考答案**：`third_party/` 存放**第三方开源库**（外部依赖）；`libraries/` 存放 asc-tools **自身构建所需的库文件**，例如 cpudebug 编译时用到的闭源 `libcpudebug_model.a`（按架构存放在 `libraries/lib/<product>/` 下）。前者是「别人的代码」，后者是「本项目专用的预编译产物」。

**练习 2**：为什么 README 目录树不完整这件事，对学习者是个「有用信号」？

> **参考答案**：它说明文档为入门做了简化，真实的「模块清单」应当以根 `CMakeLists.txt` 的 `add_subdirectory` 为准。学会看构建文件，比死记文档更可靠。

---

### 4.2 CMake 子目录组织

#### 4.2.1 概念说明

光知道目录放什么还不够，我们还要知道**这些目录是怎么被「串」成一个可编译整体的**。答案就在根 `CMakeLists.txt` 里：它通过一连串 `add_subdirectory(目录)` 把每个工具模块挂进构建系统。

可以把根 `CMakeLists.txt` 想象成「**总装车间的流水线开关**」：每写一行 `add_subdirectory(xxx)`，就相当于「把 xxx 这个模块送上流水线」。开关的顺序，就是模块被纳入构建的顺序。

#### 4.2.2 核心流程

根 `CMakeLists.txt` 末尾的一段，构成了模块组装的核心。它的逻辑用伪代码表示如下：

```
add_subdirectory(cpudebug)                  # 1. C++ 核心：编译 cpu debug 库
add_subdirectory(npuchk)                    # 2. 安装 npuchk 脚本
add_subdirectory(utils/msobjdump)           # 3. 安装 msobjdump
add_subdirectory(utils/optype_collector)    # 4. 安装 optype_collector
add_subdirectory(utils/templates)           # 5. 安装工程模板
if (开源构建 且 未开测试):
    add_subdirectory(third_party)           # 6. 条件性挂入第三方库
add_subdirectory(utils/show_kernel_debug_data)  # 7. 安装 show_kernel_debug_data
if (开启测试):
    add_subdirectory(tests)                 # 8. 条件性挂入测试
```

注意三个细节：

1. **C++ 模块在前，Python 模块在后**：`cpudebug` 排第一，因为它是真正的编译大头；Python 工具主要是「安装脚本到指定目录」，开销小。
2. **条件性挂入**：`third_party` 只在「开源构建且未开测试」时挂入；`tests` 只在 `ENABLE_TEST` 打开时挂入。这说明仓库支持「**正常构建**」和「**测试构建**」两种模式。
3. **`utils/` 下的三个工具各自独立挂入**：它们没有共用一个 `utils/CMakeLists.txt`，而是由根直接 `add_subdirectory(utils/xxx)`，体现了 Python 工具的**彼此解耦**。

#### 4.2.3 源码精读

仓库根构建文件的模块组装部分，参见 [CMakeLists.txt:L59-L70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L59-L70)。其中第 59 行 `add_subdirectory(cpudebug)` 是整个仓库最重的编译单元，第 64–66 行和第 68–70 行分别是 `third_party` 与 `tests` 的条件挂入。

再看 `cpudebug` 内部是怎么继续向下组织的，参见 [cpudebug/CMakeLists.txt:L33-L52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L33-L52)。这一段揭示了 cpu debug 的源码骨架：

- 第 33 行定义了 `PRODUCT_TYPE_LIST`（`ascend910 / ascend310p / ascend910B1 / ascend310B1 / ascend950pr_9599`），说明 cpudebug 会**为每一种 NPU 架构分别编译一份库**。
- 第 44–46 行用 `file(GLOB ... src/api_check/*.cpp)` 收集「API 校验」的全部源码。
- 第 47–52 行显式列出 `src/regfwk/` 下的四个源文件（`kernel_print_lock.cpp / stub_backtrace.cpp / stub_base.cpp / stub_reg.cpp`）。
- 第 42 行 `add_subdirectory(src/acl_stub)` 则把 ACL stub 子模块单独挂入。

也就是说，`cpudebug/src/` 下其实有 **三个职能子目录**：`acl_stub`（ACL 接口桩）、`api_check`（API 校验）、`regfwk`（注册框架）。这三个子目录是后续进阶讲义的主角。

测试目录的分发逻辑同样清晰，参见 [tests/CMakeLists.txt:L25-L31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/CMakeLists.txt#L25-L31)：根据变量 `TEST_MOD` 取值为 `cpp / python / all`，分别挂入 `ut`（C++ 测试）或 `py_ut`（Python 测试）。

#### 4.2.4 代码实践

**实践目标**：不运行任何编译，只通过阅读 CMake 文件，画出仓库的「模块挂入图」。

**操作步骤**：

1. 打开 [CMakeLists.txt:L59-L70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L59-L70)。
2. 把每一行 `add_subdirectory` 画成一个方框，条件挂入（`if` 包裹的）用虚线框。
3. 在 `cpudebug` 方框下，再展开 `acl_stub / api_check / regfwk` 三个子方框（依据 4.2.3）。

**需要观察的现象**：你会看到一张「根 → 工具模块 → 工具内部子模块」的树形图。

**预期结果**：图中 C++ 侧（`cpudebug`）有较深的子结构，Python 侧（`npuchk`、`utils/*`）都是叶子节点，直观体现「C++ 厚、Python 薄」的结构差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tests` 目录的挂入是条件性的（`if(ENABLE_TEST)`），而不是默认挂入？

> **参考答案**：测试只在开发/CI 阶段需要。普通用户安装 asc-tools 时不需要编译测试，条件挂入可以让发布构建更轻、更快。这是 CMake 项目中常见的「生产构建 vs 测试构建」分离做法。

**练习 2**：`cpudebug` 为什么要把 `api_check` 的源码用 `file(GLOB ... *.cpp)` 收集，而 `regfwk` 却逐个文件显式列出？

> **参考答案**：`api_check` 下校验器众多且会增减，用通配符可以自动收纳新增的 `.cpp`，维护成本低；`regfwk` 只固定有四个文件，显式列出更精确、可读性更好，也能避免误纳入临时文件。两种写法各有适用场景。

---

### 4.3 工具源码定位

#### 4.3.1 概念说明

前面两节是「看地图」，这一节是「找门牌」。当你想阅读某个工具的代码时，第一件事就是找到它的**主入口文件**——也就是「程序从这里开始执行」的那个文件。

不同类型的工具，入口的形态不同：

- **Python CLI 工具**：入口通常是 `__main__.py`，它被 `python -m 包名` 调用，内部再调用 `xxx_main.py` 里的主函数。
- **单文件 Python 脚本**：入口就是那个脚本本身。
- **C++ 库**：没有「命令行入口」，但有两类「入口」——**构建入口**（`CMakeLists.txt`）和**用户侧 API 入口**（对外暴露的头文件，例如 `cpu_debug_launch.h`）。

#### 4.3.2 核心流程

定位一个工具主入口的通用步骤：

1. 根据工具名，回忆它属于「C++ 核心」还是「Python 工具」。
2. 若是 Python 工具，进入对应目录，找 `__main__.py`（包入口）或唯一的 `.py` 脚本（单文件）。
3. 若是 C++ 库，先看 `CMakeLists.txt`（构建入口），再看 `include/` 下对外暴露的头文件（API 入口）。
4. 顺藤摸瓜：`__main__.py` → `xxx_main.py` → 具体实现。

#### 4.3.3 源码精读

下面把五个工具的主入口逐一锁定。

**① cpudebug（C++ 核心）**

构建入口是 [cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt)，它负责把 `src/acl_stub + src/api_check + src/regfwk` 的源码与闭源 `libcpudebug_model.a` 合并成最终的 `libcpudebug.so`。

用户侧 API 入口是头文件 `cpudebug/include/cpu_debug_launch.h`——算子源码里写 `#ifdef ASCENDC_CPU_DEBUG` 时引用的就是它。源码内部进一步分为三个子目录：

| 子目录 | 职责 |
| --- | --- |
| `src/acl_stub` | ACL 接口桩实现（如 `ascendc_acl_stub.cpp`、`kernel_fp16.cpp`） |
| `src/api_check` | API 参数校验器（`kernel_base_check.cpp`、`kernel_data_copy_check.cpp` 等） |
| `src/regfwk` | 注册框架（`stub_reg.cpp`、`stub_base.cpp`、`stub_backtrace.cpp` 等） |

**② npuchk（Python，单文件）**

npu check 的实现非常精简，整个工具就一个脚本 [npuchk/ascendc_npuchk_report.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py)，负责解析 `*_npuchk.log` 并把错误地址映射回源码行。它的安装规则见 [npuchk/CMakeLists.txt:L10-L21](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/CMakeLists.txt#L10-L21)，只是把这个 `.py` 安装到 `tools/ascendc_tools/` 目录。

**③ msobjdump（Python，包）**

命令行入口是 [utils/msobjdump/msobjdump/__main__.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/__main__.py)。它只做一件事：调用 `msobjdump_main.py` 里的 `parse_args()`，再执行 `args.entry_function(args)`。真正解析 ELF 的逻辑在同目录的 `msobjdump_main.py` 与 `utils.py` 中。目录下还有一个 `msobjdump.sh`，是给命令行直接调用的 shell 包装。

**④ show_kernel_debug_data（Python，包）**

命令行入口是 [utils/show_kernel_debug_data/show_kernel_debug_data/__main__.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/__main__.py)。它直接调用 `dump_parser.execute_parse()`，因此真正的解析实现就在同目录的 `dump_parser.py`，辅以 `data_converter.py`（数据类型转换）和 `dump_logger.py`（日志）。

**⑤ optype_collector（Python，包）**

命令行入口是 [utils/optype_collector/optype_collector/__main__.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/optype_collector/optype_collector/__main__.py)。它调用 `optype_collector_main.main()`，所以采集与冲突检测的核心逻辑在同目录的 `optype_collector_main.py` 中。

把以上五个工具汇总成一张「定位速查表」：

| 工具 | 语言 | 主入口文件 | 核心实现文件 |
| --- | --- | --- | --- |
| cpudebug | C++ | `cpudebug/CMakeLists.txt`（构建）+ `cpudebug/include/cpu_debug_launch.h`（API） | `cpudebug/src/{acl_stub,api_check,regfwk}` |
| npuchk | Python | `npuchk/ascendc_npuchk_report.py` | 同入口（单文件） |
| msobjdump | Python | `utils/msobjdump/msobjdump/__main__.py` | `utils/msobjdump/msobjdump/msobjdump_main.py` |
| show_kernel_debug_data | Python | `utils/show_kernel_debug_data/show_kernel_debug_data/__main__.py` | `utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py` |
| optype_collector | Python | `utils/optype_collector/optype_collector/__main__.py` | `utils/optype_collector/optype_collector/optype_collector_main.py` |

> 规律总结：Python 工具几乎都遵循 `__main__.py`（入口）→ `xxx_main.py`（实现）的二层结构。记住这个规律，以后找任何一个 Python 工具的代码都能一击即中。

#### 4.3.4 代码实践

**实践目标**：独立定位五个工具的主入口，并验证 Python 工具「入口 → 实现」的调用链。

**操作步骤**：

1. 在本地仓库执行下列操作，逐一打开五个工具的入口文件：
   - `cpudebug`：打开 `cpudebug/CMakeLists.txt` 与 `cpudebug/include/cpu_debug_launch.h`。
   - `npuchk`：打开 `npuchk/ascendc_npuchk_report.py`。
   - `msobjdump`：打开 `utils/msobjdump/msobjdump/__main__.py`。
   - `show_kernel_debug_data`：打开 `utils/show_kernel_debug_data/show_kernel_debug_data/__main__.py`。
   - `optype_collector`：打开 `utils/optype_collector/optype_collector/__main__.py`。
2. 对三个 Python 包工具，在 `__main__.py` 里找到它 `import` 的主模块（分别是 `msobjdump_main`、`dump_parser`、`optype_collector_main`），然后打开对应的实现文件，确认入口调用的函数确实存在于实现文件中。

**需要观察的现象**：

- 每个 `__main__.py` 都非常短（通常不到 10 行有效代码），只负责「解析参数 / 调用主函数」。
- 三个 Python 包的 `__main__.py` 都形如 `from xxx.yyy import f` 然后 `f()`，结构高度一致。

**预期结果**：你能用一句话说出每个工具「从哪个文件开始、调到哪里」，并验证 `import` 的函数名在实现文件中真实存在。

> 说明：本实践为「源码阅读型实践」，不要求运行命令。若想进一步验证，可在配置好 Python 环境后用 `python -m msobjdump --help`（在 `utils/msobjdump` 目录下）观察是否触发了 `__main__.py`，运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：cpudebug 是 C++ 库，没有 `__main__.py`，那它的「入口」该怎么理解？

> **参考答案**：C++ 库有两类入口——**构建入口**是 `cpudebug/CMakeLists.txt`，它决定编译哪些源码、链接哪些库、产出 `libcpudebug.so`；**用户侧 API 入口**是 `cpudebug/include/cpu_debug_launch.h`，算子源码通过 `#include` 它来获得 CPU 域启动能力。理解 C++ 库时，要同时关注「怎么编」和「怎么被调用」。

**练习 2**：如果你要给 `msobjdump` 新增一个命令行选项，应该改哪个文件？为什么？

> **参考答案**：主要改 `utils/msobjdump/msobjdump/__main__.py` 所调用的 `msobjdump_main.py`（参数解析与命令分发都在这里）。`__main__.py` 只是薄薄一层调用壳，真正的 `parse_args()` 和 `entry_function` 都定义在 `msobjdump_main.py` 中。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「仓库导览」小任务：

**任务**：假设有位新同事问你「我想看看 npu check 是怎么把错误地址翻译成源码行的，代码在哪？」请用本讲学到的方法，写出一条「定位路径」。

**建议步骤**：

1. **判断类型**：npu check 属于 Python 工具（依据 4.1 的分组）。
2. **找目录**：根据 [README.md:L38](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/README.md#L38)，npu check 的代码在 `npuchk/`。
3. **找入口**：进入 `npuchk/`，发现只有一个 `ascendc_npuchk_report.py`（单文件脚本，依据 4.3.3 ②）。
4. **找函数**：打开该文件，搜索 `addr2line` 或 `addr_to_line` 之类的函数名，定位「地址 → 源码行」的实现。

**预期产出**：一条清晰的回答，例如「`npuchk/ascendc_npuchk_report.py`，搜索 `addr_to_line` 相关函数，即可看到用 `addr2line` 把地址翻译成源码行的逻辑」。这个练习同时复现了「目录结构 → 入口定位 → 函数定位」的完整找路流程。

> 进阶（可选）：用同样的三步法，分别定位「msobjdump 解析 ELF 段」「show_kernel_debug_data 解析 dump bin」「optype_collector 检测冲突」的实现函数所在文件，做成你自己的速查表。

---

## 6. 本讲小结

- 仓库顶层目录按**职责分组**：`cpudebug/` 是 C++ 核心，`npuchk/` 与 `utils/` 下放 Python 工具，`cmake/ · build.sh · CMakeLists.txt` 负责构建，`docs/ · examples/ · tests/ · libraries/ · third_party/ · scripts/` 提供辅助。
- asc-tools 的本质结构是 **「一个 C++ 核心（cpudebug）+ 四个 Python 工具」**，C++ 厚、Python 薄，二者在目录上严格分离。
- 根 [CMakeLists.txt:L59-L70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L59-L70) 用一连串 `add_subdirectory` 把各模块挂入构建，其中 `tests` 与 `third_party` 是条件挂入。
- `cpudebug/src/` 内部细分为 `acl_stub`（ACL 桩）、`api_check`（API 校验）、`regfwk`（注册框架）三个职能子目录，并为多种 NPU 架构分别编译。
- README 目录树**不一定完整**（如 `utils/` 下的 `optype_collector`、`templates` 未列出），真实模块清单以根 `CMakeLists.txt` 为准。
- Python 工具普遍遵循 **`__main__.py`（入口）→ `xxx_main.py`（实现）** 的二层结构；记住这个规律即可快速定位任意 Python 工具的代码。

---

## 7. 下一步学习建议

本讲让你拿到了仓库的「地图和门牌」。接下来的学习建议：

1. **先把环境搭起来**：进入第 1 单元的 [u1-l3 开发环境搭建与依赖管理](u1-l3-environment-setup.md)，了解编译运行 asc-tools 需要哪些依赖（CANN 包、gcc、cmake、python 等）。
2. **再跑通第一个样例**：接着读 [u1-l4 一键编译与运行第一个样例](u1-l4-build-and-first-sample.md)，亲手执行 `build.sh`，把本讲看到的目录「变」成实际的编译产物。
3. **想深入 cpudebug 的源码**：在跑通样例后，可以进入第 3 单元（cpudebug 核心运行机制），从 `cpudebug/src/regfwk/stub_reg.cpp` 和 `cpudebug/include/kern_fwk.h` 开始，看 C++ 核心内部是如何工作的。
4. **持续使用本讲的「定位速查表」**：后续每读到一篇工具讲义，都回到本讲的 4.3.3 节，对照确认该工具的入口与实现文件，巩固「找路」能力。
