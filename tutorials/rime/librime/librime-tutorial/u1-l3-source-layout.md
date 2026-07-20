# 源码目录与分层架构

## 1. 本讲目标

上一篇（u1-l2）我们解决了「librime 的 C++ 源码如何被编译成库」。本篇要回答的是下一个更根本的问题：**这套庞大的 C++ 代码到底是怎么组织的？我该从哪里开始读？**

读完本讲，你应当能够：

- 在脑子里建立一张 librime 仓库的全景地图，知道每个顶层目录（`src`、`include`、`data`、`tools`、`sample`、`plugins` 等）各干什么。
- 说出 `src/rime/` 下五大子目录 `algo`、`config`、`dict`、`gear`、`lever` 的职责分工，并能为每个目录举出一个代表文件。
- 区分**公共头文件**（`src/*.h`，安装给前端调用方）与**内部头文件**（`src/rime/*.h`，引擎内部使用）。
- 理解 `src/rime/common.h` 这个「全局基础设施」头文件提供的能力（智能指针别名、`path`、日志开关等），因为后面几乎每个 `.cc` 都会 `#include <rime/common.h>`。
- 知道 `data/minimal/` 提供了让引擎跑起来的最小数据集。

本讲**不深入任何具体算法或组件实现**，只画地图。具体的运行时对象（Engine / Context / Service 等）从下一篇 u1-l4 起再逐步展开。

## 2. 前置知识

阅读本讲前，建议你已经具备：

- **C++ 基本常识**：知道什么是头文件（`.h`）与源文件（`.cc`）、什么是 `namespace`、什么是智能指针（`std::shared_ptr` / `std::unique_ptr`）。
- **CMake 基本常识**：知道 `add_library`、`add_subdirectory`、`target_link_libraries` 这些命令的作用。上一篇 u1-l2 已经讲过 librime 的构建选项与依赖，本篇会复用其中关于「库产物」的结论。
- **本手册前两篇的认知**：librime 是「引擎」（只做按键→文字的计算），前端（Squirrel/Weasel 等）负责界面；一个**输入方案（schema）**就是一份 YAML，换方案即换输入法。

一个贯穿全篇的核心直觉是：**librime 把不同关注点（算法、配置、字典、流水线组件、部署）放进不同子目录，编译时再按「模块组」拼装成一个（或多个）库**。目录划分 ≈ 关注点划分，这也是我们读源码时的天然导航。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 作用 |
|------|------|
| `CMakeLists.txt`（仓库根） | 顶层构建脚本：定义选项、查找依赖、装配子目录、安装公共头。 |
| `src/CMakeLists.txt` | 把 `src/rime/` 各子目录的源文件分组、组装成 `rime` 库。 |
| `src/rime/common.h` | 全局基础设施：标准库别名、智能指针别名、`path` 类、日志开关。 |
| `src/rime/build_config.h.in` | 构建期模板，CMake 渲染后生成 `build_config.h`，供源码 `#ifdef` 消费。 |
| `src/*.h` | 公共头文件（`rime_api.h` 等），安装给前端调用方。 |
| `src/rime/*.h`、`src/rime/algo/`、`config/`、`dict/`、`gear/`、`lever/` | 引擎内部头文件与实现，按关注点分目录。 |
| `include/` | 捆绑的第三方单头/内联库：`darts.h`（双数组 trie）、`utf8.h`、X11 键码头。 |
| `data/minimal/` | 最小可运行数据集：`default.yaml`、`luna_pinyin` 方案与词典、符号表等。 |
| `tools/` | 命令行工具（`rime_api_console` 等），用于体验与部署。 |
| `sample/` | 示例插件，演示如何为 librime 写自定义组件。 |
| `plugins/` | 插件加载框架（`plugins_module.cc`）。 |

## 4. 核心概念与源码讲解

### 4.1 全景：从仓库根到 `src/rime`

#### 4.1.1 概念说明

打开 librime 仓库根目录，你会看到一堆目录。为了不迷路，我们先把它们按「角色」分成五类：

1. **构建与元信息**：`CMakeLists.txt`、`cmake/`（CMake 查找模块 `Find*.cmake`）、`rime.pc.in`（pkg-config 模板）、`*.sh`（安装/版本脚本）、`CHANGELOG.md`、`README*.md`。
2. **源码本体**：`src/`（引擎全部 C++ 实现 + 公共头）、`include/`（捆绑的第三方内联库）。
3. **数据**：`data/minimal/`（最小数据集）、`data/test/`（测试用数据）。
4. **可执行工具**：`tools/`（命令行程序）、`sample/`（示例插件，可独立编译）。
5. **扩展机制**：`plugins/`（插件加载框架）、`test/`（单元/集成测试）。

另外有 `bin/`、`lib/`、`share/`、`deps/` 几个「占位/产出」目录：`deps/` 供 `make deps`（见 u1-l2 的 `deps.mk`）放置自建的静态依赖；根目录的 `lib/` 会被 CMake 用 `link_directories` 引用，用来放置预编译的第三方库；`bin/`、`share/` 通常是安装或运行时的占位输出位置。本讲重点是**源码本体**这一类。

#### 4.1.2 核心流程：源码如何被组装成库

从「源码目录」到「库产物」，CMake 的组装逻辑可以概括为三步：

1. **顶层 `CMakeLists.txt`** 负责配置：定义选项、查找依赖、把构建期变量渲染进 `build_config.h`、设置头文件搜索路径、`add_subdirectory()` 进入各子目录。
2. **`src/CMakeLists.txt`** 负责「分组」：用 `aux_source_directory` 把 `src/rime/` 各子目录的 `.cc` 收集成若干变量，再按模块组拼成最终的源文件列表。
3. 最后 `add_library(rime ...)` 生成库，`target_link_libraries` 把对应依赖挂上去。

一个关键观察是：**目录划分与依赖分组是一一对应的**。下面这张「目录 → 模块组 → 依赖」对照表，是理解整个仓库结构最实用的一张表：

| `src/rime/` 子目录 | 在 `src/CMakeLists.txt` 中对应的源文件变量 | 主要链接的第三方依赖 |
|---|---|---|
| 根级文件（`engine.cc`、`context.cc`、`service.cc` 等） | `rime_base_src` | Boost / Glog / YamlCpp（合称 `rime_core_deps`） |
| `config/` | `rime_config_src` | （并入 core） |
| `algo/` + `dict/` | `rime_dict_module_src` | LevelDB / marisa（`rime_dict_deps`） |
| `gear/` | `rime_gears_src` | OpenCC / ICU（`rime_gears_deps`） |
| `lever/` | `rime_levers_src` | （`rime_levers_deps`，当前为空） |

这也解释了为什么 `dict` 子目录会依赖 LevelDB 与 marisa（用户词典与音节索引），而 `gear` 子目录会依赖 OpenCC（繁简转换）：**目录 = 关注点 = 依赖面**。

#### 4.1.3 源码精读

顶层 `CMakeLists.txt` 设置了三处头文件搜索路径，其中 `src` 让源码能用 `#include <rime/common.h>` 这种写法，`include` 让源码能用捆绑的 `darts.h`、`utf8.h`：

- [CMakeLists.txt:L164-L166](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L164-L166) — 三个 `include_directories`：构建目录（含生成的 `build_config.h`）、`src`（公共头与内部头根）、`include`（捆绑第三方库）。

顶层脚本用 `add_subdirectory` 依次进入插件、源码、工具、测试、示例：

- [CMakeLists.txt:L264-L288](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L264-L288) — `add_subdirectory(plugins)`、`add_subdirectory(src)`，并在 `BUILD_SHARED_LIBS` 开启时才进入 `tools`、`test`、`sample`。这也是 u1-l2 提到的「静态库构建时不产出工具/测试」的实现位置。

`src/CMakeLists.txt` 用 `aux_source_directory` 把每个子目录收成一个变量，这是「目录 → 源文件变量」映射的源头：

- [src/CMakeLists.txt:L1-L7](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L1-L7) — `rime_base_src`、`rime_algo_src`、`rime_config_src`、`rime_dict_src`、`rime_gears_src`、`rime_levers_src`。

随后把变量按模块组组合：

- [src/CMakeLists.txt:L12-L15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L12-L15) — `rime_core_module_src = rime_api_src + rime_base_src + rime_config_src`（API + 引擎骨架 + 配置）。
- [src/CMakeLists.txt:L22-L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L22-L24) — `rime_dict_module_src = rime_algo_src + rime_dict_src`（算法 + 字典）。
- [src/CMakeLists.txt:L29-L36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L29-L36) — 默认情况下把 core + dict + gears + levers + plugins 全部合并进 `rime_src`，编译成单一动态库；只有 `BUILD_SEPARATE_LIBS=ON` 时才拆分。

依赖也按同样的分组挂载：

- [src/CMakeLists.txt:L46-L58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L46-L58) — `rime_core_deps`（Boost/Glog/YamlCpp/Threads）、`rime_dict_deps`（LevelDB/Marisa）、`rime_gears_deps`（ICU/OpenCC）、`rime_levers_deps`（空）。

最终生成默认的单一动态库 `rime`：

- [src/CMakeLists.txt:L82-L94](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L82-L94) — `add_library(rime ${rime_src})`，设置 `VERSION`/`SOVERSION`、输出到 `build/lib`、安装到系统库目录。这与 u1-l2 讲过的「默认产物是 `build/lib/librime.so`」完全对应。

#### 4.1.4 代码实践

**实践目标**：在源码层面验证「目录 → 模块组 → 库」的映射，确认你读对了地图。

**操作步骤**：

1. 打开 [src/CMakeLists.txt:L1-L7](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L1-L7)，逐行核对每个 `aux_source_directory(子目录 变量名)`。
2. 打开 [src/CMakeLists.txt:L29-L36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L29-L36)，看默认分支把哪些变量拼进 `rime_src`。
3. 对照 [src/CMakeLists.txt:L46-L58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L46-L58) 的依赖分组，回答下面的问题。

**需要观察的现象 / 预期结果**：

- 你应能确认：`gear/` 子目录的代码（如繁简转换 `simplifier.cc`）之所以能用 OpenCC，是因为它被归入 `rime_gears_src`，而该组链接了 `rime_gears_deps`（含 `${Opencc_LIBRARY}`）。
- 你应能确认：默认（非 `BUILD_SEPARATE_LIBS`）情况下，所有子目录最终都进入同一个 `rime` 库，这也是为什么前端只需要链接一个 `rime` 就够了。

> 如果你想亲眼看到分组效果，可在本地执行 `cmake -B build -DBUILD_SEPARATE_LIBS=ON` 后查看 `build/CMakeCache.txt` 中是否出现 `rime-dict`/`rime-gears`/`rime-levers` 目标。**待本地验证**（取决于环境是否装齐依赖）。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `algo/` 和 `dict/` 合并成同一个 `rime_dict_module_src`，而不是各自独立？

**参考答案**：因为 `algo/`（拼写代数、音节切分、编码生成等算法）几乎只为 `dict/`（词典构建与查询）服务——音节切分依赖 Prism、Prism 又是 dict 的产物，二者紧密耦合且共用 LevelDB/marisa 这类依赖，所以合并成一个模块组既能反映真实耦合关系，又能避免循环依赖。

**练习 2**：如果想做一个「只带引擎骨架、不带任何具体 Processor/Translator」的精简库，应该保留哪些源文件变量、去掉哪些？

**参考答案**：保留 `rime_api_src` + `rime_base_src` + `rime_config_src`（即 `rime_core_module_src`），去掉 `rime_gears_src`（具体流水线组件全在 `gear/`）。代价是没有任何可用的 Processor/Segmentor/Translator/Filter 实现，引擎装配时会找不到组件——所以这只在二次开发「换一套组件」时才有意义。

---

### 4.2 核心模块一：`src/rime/` 的五大子目录

这是本讲最重要的地图。`src/rime/` 根目录下除了大量「核心类」文件（`engine.*`、`context.*`、`service.*` 等）之外，还有五个子目录，分别承载五类关注点。CMake 在安装私有头时，正是按这五个目录遍历的：

- [CMakeLists.txt:L239-L244](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L239-L244) — `foreach(rime_private_header_files_dir algo config dict gear lever)`，这行就是五大子目录的「权威清单」。

#### 4.2.1 概念说明：五大子目录各管什么

| 子目录 | 关注点 | 一句话职责 | 代表文件 |
|---|---|---|---|
| `algo/` | **算法** | 与「输入串如何被解析」相关的纯算法：拼写代数、音节切分、编码生成、字符串工具。 | `algo/calculus.h`（拼写代数运算注册表） |
| `config/` | **配置** | YAML 配置的数据模型、加载、编译器（`__include`/`__patch` DSL）与各类配置插件。 | `config/config_types.h`（配置树节点类型） |
| `dict/` | **字典** | 词典从 `.dict.yaml` 到 `.prism.bin`/`.table.bin` 的编译、查询、用户词典（LevelDB）。 | `dict/dict_compiler.h`（词典编译器） |
| `gear/` | **齿轮组件** | 引擎流水线上所有「具体」组件的实现：各类 Processor / Segmentor / Translator / Filter。 | `gear/speller.h`（拼写处理器） |
| `lever/` | **部署杠杆** | 部署期任务（构建词典、同步用户数据、定制设置）、方案切换器设置。 | `lever/deployment_tasks.h`（部署任务族） |

一个形象的类比：`algo/` 是「数学公式」，`config/` 是「读设置」，`dict/` 是「查字典」，`gear/` 是「流水线上的工人」，`lever/` 是「上线/维护工具」。运行时一次按键主要穿过的就是 `gear/` 里的工人（以及它们调用的 `algo/` 公式和 `dict/` 查询）；`lever/` 主要在「部署（deploy）」阶段被调用。

#### 4.2.2 核心流程：根级核心类 vs 子目录组件

除了五个子目录，`src/rime/` 根下还有一批**核心类**文件，它们定义了引擎运行时的「骨架对象」。理解根级文件与子目录的关系，是把握整个架构的关键：

```
src/rime/                          ← 引擎内部实现（不直接暴露给前端）
├── 根级核心类（骨架）
│   ├── engine.*        引擎：持有 Schema + Context，驱动流水线
│   ├── context.*       输入状态容器（原始输入、光标、Composition）
│   ├── service.*       Service 单例：管理多个 Session
│   ├── schema.*        输入方案 = schema_id + Config
│   ├── segmentation.*  输入串切分成 Segment 序列
│   ├── composition.*   Composition：包装 Segmentation + 候选
│   ├── candidate.*     候选项数据模型
│   ├── translation.*   候选拉取迭代器
│   ├── menu.*          候选分页
│   ├── key_event.*     按键的内部表示（keycode + modifier）
│   ├── key_table.*     键名 ↔ keycode 映射表（大表，~79KB）
│   ├── processor.h / segmentor.h / translator.h / filter.h / formatter.h
│   │                   ← 四大组件基类 + Formatter（流水线契约）
│   ├── component.h / registry.*   组件注册体系
│   ├── module.* / setup.*         模块机制与初始化
│   ├── deployer.*      部署器（lever 任务调度者）
│   ├── switcher.* / switches.*    方案/开关切换
│   ├── ticket.*        组件实例化上下文
│   ├── resource.* / signature.* / messenger.* / language.* / commit_history.*
│   └── core_module.cc  core 模块：注册基础组件
├── algo/   ← 算法（拼写代数、音节切分、编码）
├── config/ ← 配置（数据模型、加载、编译器 DSL、插件）
├── dict/   ← 字典（Prism/Table/DictCompiler/用户词典）
├── gear/   ← 齿轮组件（流水线上所有具体 Processor/Segmentor/Translator/Filter）
└── lever/  ← 部署杠杆（部署任务、定制设置、切换器设置）
```

记住一条主线：**根级文件定义「抽象骨架与契约」，子目录（尤其 `gear/`）提供「具体实现」**。例如 `processor.h` 只声明了 `Processor` 抽象基类与 `ProcessKeyEvent` 的返回值约定，而 `gear/speller.cc`、`gear/selector.cc` 等才是具体实现。这也是为什么 u6（按键处理流水线）会先讲基类（u5-l2），再逐个讲 `gear/` 里的实现（u6-l2 ~ u6-l5）。

#### 4.2.3 源码精读：五个代表文件

下面给每个子目录举一个代表文件，确认上面的职责描述。

**algo/ — `calculus.h`（拼写代数运算）**：定义了 `Calculus` 注册表与各种 `Calculation` 子类（`xform`/`derive`/`erase`/`fuzz`/`abbrev`/`xlit`），用来从基础拼写派生出模糊音、缩写等变体：

- [src/rime/algo/calculus.h:L19-L28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.h#L19-L28) — `Calculation` 抽象基类，核心是 `virtual bool Apply(Spelling* spelling)`，即「对一条拼写施加一次运算」。

**config/ — `config_types.h`（配置数据模型）**：定义配置树的节点类型层次 `ConfigItem`（基类）→ `ConfigValue` / `ConfigList` / `ConfigMap`：

- [src/rime/config/config_types.h:L17-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L17-L32) — `ConfigItem` 用 `ValueType { kNull, kScalar, kList, kMap }` 区分四种节点类型，这就是 YAML 在内存中的表示。

**dict/ — `dict_compiler.h`（词典编译器）**：把 `.dict.yaml` 编译成 `.prism.bin` / `.table.bin` 等二进制产物：

- [src/rime/dict/dict_compiler.h:L25-L52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h#L25-L52) — `DictCompiler` 暴露 `Compile(schema_file)` 入口，内部私有方法 `BuildTable` / `BuildPrism` / `BuildReverseDb` 对应三类构建产物。

**gear/ — `speller.h`（拼写处理器）**：一个具体的 `Processor` 实现，把合法按键追加进 `Context::input`：

- [src/rime/gear/speller.h:L20-L49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.h#L20-L49) — `Speller : public Processor`，重写 `ProcessKeyEvent`，持有 `alphabet_`、`delimiters_`、`max_code_length_` 等来自方案 `speller` 段的配置。

**lever/ — `deployment_tasks.h`（部署任务族）**：一组 `DeploymentTask` 子类，每个负责一项部署工作（检测改动、更新工作区、构建词典、同步用户数据等）：

- [src/rime/lever/deployment_tasks.h:L16-L97](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.h#L16-L97) — `DetectModifications` / `WorkspaceUpdate` / `SchemaUpdate` / `ConfigFileUpdate` / `PrebuildAllSchemas` / `UserDictUpgrade` / `UserDictSync` 等任务类，注释里写明了各自职责。

#### 4.2.4 代码实践

**实践目标**：用源码自证「五大子目录」的划分，并为每个目录找到一个代表文件。

**操作步骤**：

1. 在仓库根执行（只读命令）：
   ```bash
   for d in algo config dict gear lever; do echo "== $d =="; ls src/rime/$d/*.h | head -5; done
   ```
2. 打开 [CMakeLists.txt:L239-L244](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L239-L244)，确认这五个名字就是 CMake 安装私有头时遍历的目录。
3. 为每个目录挑一个代表文件，按本节给出的「代表文件」对照阅读其顶部注释与第一个类声明。

**需要观察的现象 / 预期结果**：

- 五个子目录确实各自聚焦一类关注点，没有交叉。例如 `gear/` 里全是 `Processor`/`Segmentor`/`Translator`/`Filter` 的具体实现，而 `algo/` 里全是与「解析输入串」相关的算法。
- 你能在每个目录里找到本节列出的代表文件（`calculus.h`、`config_types.h`、`dict_compiler.h`、`speller.h`、`deployment_tasks.h`）。

#### 4.2.5 小练习与答案

**练习 1**：`gear/speller.cc` 依赖了 `algo/` 里的东西吗？为什么它们被放在不同目录？

**参考答案**：会依赖。`speller` 把按键追加成输入串后，最终要交给 `script_translator` 等翻译器，而翻译器会调用 `algo/syllabifier`（音节切分）和 `algo/calculus`（拼写代数）来解析输入。它们分属不同目录是因为关注点不同：`gear/` 是「流水线组件」，`algo/` 是「被组件复用的纯算法」。把算法抽出来还能被 `dict/`（构建 Prism 时也用拼写代数）复用。

**练习 2**：为什么 `switcher.*`、`switches.*` 在 `src/rime/` 根级，而 `switcher_settings.*` 在 `lever/` 子目录？

**参考答案**：`switcher.h/cc`、`switches.h/cc` 是**运行时**对象——Switcher 本身是一个特殊的 Engine/Processor，开关状态在每次按键时被读取，所以放在根级核心类里；而 `lever/switcher_settings.*` 是**部署/定制期**的设置管理（如何持久化、如何被 `*.custom.yaml` 修改），属于 lever 关注的「维护与定制」，所以放进 `lever/`。同一个概念在「运行时」和「部署时」有两套代码，分别落在不同目录。

---

### 4.3 核心模块二：`common.h` 全局基础设施

`src/rime/common.h` 是整个引擎里被 `#include` 最多的头文件之一——几乎所有 `.cc` 第一行都是 `#include <rime/common.h>`。它不实现任何输入法功能，而是提供一套**贯穿全仓库的「便利设施」**：标准库类型别名、智能指针别名、信号槽、路径类型、日志开关。

#### 4.3.1 概念说明

读 librime 源码时你会反复看到一些「奇怪」的写法，比如 `an<Dictionary>`、`the<Prism>`、`New<Engine>(...)`、`of<T>`、`weak<T>`、`Is<Candidate>(ptr)`、`As<ScriptTranslator>(ptr)`、以及随处可见的 `signal<>` 和 `rime::path`。它们全都定义在 `common.h` 里。理解这些别名，是顺畅阅读后续每一篇讲义的前提。

这些别名的设计意图是：

- **用短名替代冗长的 STL 全限定名**：`string`、`vector`、`map`、`set`、`list`、`deque`、`function`，避免到处写 `std::`。
- **用英文冠词命名智能指针，提升可读性**：`the<T>` = `unique_ptr<T>`（独占，「这个」），`an<T>` = `shared_ptr<T>`（共享，「一个」），`of<T>` = `an<T>`（`of` 是 `an` 的别名，常用于容器内元素，读起来像 `vector<of<Candidate>>`）。
- **统一哈希容器**：`hash_map`/`hash_set` 用 `boost::unordered_*`（早期 C++ 标准库的 `unordered_map` 性能/兼容性不如 Boost 版本）。
- **统一路径类型**：自定义 `rime::path` 继承自 `std::filesystem::path`，额外处理 Windows 下 UTF-8 路径转换。

#### 4.3.2 核心流程：`common.h` 如何被消费

`common.h` 的依赖链条是这样的：

1. 它先 `#include <rime/build_config.h>`（构建期生成，携带 `RIME_ENABLE_LOGGING` 等宏）。
2. 再引入标准库头与 Boost 头（`signals2`、`unordered`）。
3. 根据 `RIME_ENABLE_LOGGING` 决定引入 `glog/logging.h` 还是内部的 `no_logging.h`。
4. 在 `namespace rime` 里 `using` 一批标准库符号、定义别名模板与 `path` 类。

于是任何 `#include <rime/common.h>` 的文件，都同时拿到了标准库别名、智能指针别名、信号槽、路径类型与（可选的）日志宏。这就是它成为「全局基础设施」的原因。

`build_config.h` 本身由模板 [src/rime/build_config.h.in](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/build_config.h.in) 经 CMake 的 `configure_file` 渲染而来，内容很少：只有 `RIME_ENABLE_LOGGING`、`RIME_ALSO_LOG_TO_STDERR`、`RIME_DATA_DIR`、`RIME_PLUGINS_DIR` 四个宏。这呼应了 u1-l2 讲过的「构建选项被 `configure_file` 渲染成 `build_config.h` 供源码 `#ifdef` 消费」。

#### 4.3.3 源码精读

**日志开关**——根据编译期宏在 glog 与空实现间切换：

- [src/rime/common.h:L28-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L28-L32) — `#ifdef RIME_ENABLE_LOGGING` 引入 glog，否则引入 `no_logging.h`（空操作）。这就是 `ENABLE_LOGGING=OFF` 时引擎仍能编译的原因。

**标准库别名**——在 `namespace rime` 内 `using`：

- [src/rime/common.h:L41-L50](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L41-L50) — `using std::string;`、`vector`、`map`、`set`、`list`、`deque`、`function`、`pair`、`make_pair`、`make_unique`。

**智能指针别名与哈希容器**——本文件最值得记住的一段：

- [src/rime/common.h:L52-L64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L52-L64) — `hash_map`/`hash_set` 指向 Boost 哈希容器；`the<T>`=`unique_ptr<T>`、`an<T>`=`shared_ptr<T>`、`of<T>`=`an<T>`、`weak<T>`=`weak_ptr<T>`。后续讲义里只要看到 `an<...>` 就理解为 `shared_ptr`。

**类型转换与工厂辅助**——读源码时到处可见的 `As`/`Is`/`New`：

- [src/rime/common.h:L66-L79](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L66-L79) — `As<X,Y>` = `dynamic_pointer_cast`（向下转型），`Is<X,Y>` = 「能否转型为 X」，`New<T>(...)` = `make_shared<T>(...)`（带完美转发）。例如判断一个候选是不是某种子类就写 `Is<Sentence>(cand)`。

**信号槽**——librime 的事件通知底座：

- [src/rime/common.h:L81-L82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L81-L82) — `using connection = boost::signals2::connection;` 和 `using signal = boost::signals2::signal;`。Context 的各类 Notifier（提交、选择、更新等）都是 `signal<>`，订阅者拿到 `connection` 来管理订阅生命周期。这部分会在 u3-l1（Context）详细展开。

**`rime::path` 类型**——处理跨平台路径：

- [src/rime/common.h:L84-L136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L84-L136) — `class path : public std::filesystem::path`，在 Windows 下用 `u8path` 把 UTF-8 字符串转成原生编码，并重载 `operator/=` 与友元 `operator/` 支持用 `string`/`char*` 直接拼接。引擎里所有文件路径（方案文件、用户词典、数据目录）都用 `rime::path`。

#### 4.3.4 代码实践

**实践目标**：把 `common.h` 的别名「翻译」回标准 C++，确认你读懂了这些短名。

**操作步骤**：

1. 打开任意一个引擎源文件，例如 [src/rime/dict/dict_compiler.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h)。
2. 找到成员声明 `an<Prism> prism_;`、`vector<of<Table>> tables_;`、`the<ResourceResolver> source_resolver_;`。
3. 对照 [src/rime/common.h:L52-L64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L52-L64)，把它们「展开」成标准库写法。

**需要观察的现象 / 预期结果**：

- `an<Prism>` → `std::shared_ptr<Prism>`（共享所有权的音节索引）。
- `vector<of<Table>>` → `std::vector<std::shared_ptr<Table>>`（多个共享的码表）。注意这里 `of<Table>` 读作「一个 Table」，整句读作「vector of Table」，十分自然。
- `the<ResourceResolver>` → `std::unique_ptr<ResourceResolver>`（独占的资源解析器）。

如果这三行你都能正确展开，说明你已经掌握了 librime 源码最常用的「语法糖」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `of<T>` 被定义为 `an<T>`（即 `shared_ptr`）的别名，而不是 `unique_ptr`？

**参考答案**：`of<T>` 主要用在容器元素位置（如 `vector<of<Table>>`、`list<of<Candidate>>`），而容器里的元素必须是**可拷贝**的（容器在扩容、拷贝时需要复制元素）。`unique_ptr` 不可拷贝（只能移动），无法直接放进 `vector`；`shared_ptr` 可拷贝，所以 `of` 选择别名到 `shared_ptr`。

**练习 2**：如果构建时 `-DENABLE_LOGGING=OFF`，引擎里 `LOG(INFO) << ...` 这类调用会怎样？

**参考答案**：此时 `RIME_ENABLE_LOGGING` 未定义，`common.h` 转而引入 `no_logging.h`（见 L31），后者把 `LOG`/`DLOG` 等宏定义为空操作，因此 `LOG(INFO)` 会被编译成无任何运行时开销的空语句。这就是 u1-l2 讲过的「glog 可选」在源码层面的落地方式。

---

### 4.4 头文件边界：公共头 vs 内部头，以及 `data/minimal`

#### 4.4.1 概念说明

librime 的头文件分成两层，对应两类读者：

- **公共头（public headers）**：位于 `src/*.h`，即 `rime_api.h`、`rime_api_deprecated.h`、`rime_api_impl.h`、`rime_api_stdbool.h`、`rime_levers_api.h`。这些是**安装给前端调用方**的头文件，定义了稳定的 C API。前端（Squirrel/Weasel 等）只 `#include <rime_api.h>` 就能用引擎。
- **内部头（private/internal headers）**：位于 `src/rime/*.h` 及其五个子目录，定义引擎内部的类（Engine、Context、各种组件）。默认**不安装**；只有开启 `INSTALL_PRIVATE_HEADERS` 时才安装，主要供「外部编译的插件」使用。

与之配套的还有 `include/` 目录里**捆绑的第三方内联库**：`darts.h`（Darts 双数组 trie，Prism 的底座）、`utf8.h`（UTF-8 处理）、`X11/keysym.h`（X11 键码，用于按键映射），以及 `COPYING.darts-clone`（Darts 的许可证）。这些不是 librime 自己的代码，而是为了避免让用户额外装这些小库而直接 vendoring 进仓库。

另外，引擎要跑起来还**需要数据**——这就是 `data/minimal/`：它提供了一套最小可运行数据集，让引擎在没有完整 RIME 数据包的情况下也能演示拼音/仓颉输入。其内容见下表。

#### 4.4.2 核心流程：头文件如何被安装与使用

公共头的安装逻辑在顶层 `CMakeLists.txt`：

- [CMakeLists.txt:L229-L232](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L229-L232) — `file(GLOB src/*.h)` 收集公共头，并用 `list(FILTER ... EXCLUDE REGEX .*_impl\.h$)` 排除 `*_impl.h`（实现细节，不安装），再 `install` 到系统 include 目录。所以 `rime_api_impl.h` 虽然在 `src/` 下，但不会安装给用户。

内部头只在 `INSTALL_PRIVATE_HEADERS=ON` 时安装：

- [CMakeLists.txt:L233-L245](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L233-L245) — 安装 `src/rime/*.h`（含构建期生成的 `build_config.h`），并 `foreach` 遍历 `algo config dict gear lever` 安装各子目录头。该选项的注释明确写着「usually needed for externally built Rime plugins」。

`data/minimal/` 的内容（最小数据集）：

| 文件 | 作用 |
|---|---|
| `default.yaml` | 全局默认配置：方案列表、开关、菜单、符号、识别器等（u4-l4 详讲）。 |
| `luna_pinyin.schema.yaml` | 「明月拼音」方案定义（schema/switches/engine/speller/translator/...）。 |
| `luna_pinyin.dict.yaml` | 明月拼音的词条源（`你好\tni hao` 这种）。 |
| `cangjie5.schema.yaml` / `cangjie5.dict.yaml` | 仓颉五代码表方案，作为形码示例。 |
| `symbols.yaml` | 符号表（标点/特殊符号的输入映射）。 |
| `essay.txt` | 字/词频语料，用于候选排序。 |

这套数据是后续 u1-l5（用 console 体验输入流程）和 u2-l3（Schema）会真实加载的内容。

#### 4.4.3 源码精读

公共头的「清单」可以直接列出来确认——`src/` 下只有 5 个 `.h`：

```
src/rime_api.h            ← C API 总入口（RimeApi 结构体），u1-l4 详讲
src/rime_api_deprecated.h  ← 已废弃的旧 API（向后兼容）
src/rime_api_impl.h        ← C++ 实现辅助（被排除安装）
src/rime_api_stdbool.h     ← stdbool 版本的 API 别名
src/rime_levers_api.h      ← 部署相关的扩展 API
```

它们与内部头的边界，由上面那条 `list(FILTER ... EXCLUDE REGEX .*_impl\.h$)` 规则划清：只有 `*_impl.h` 不算公共 API。

`data/minimal/default.yaml` 与 `luna_pinyin.schema.yaml` 的结构会在 u2-l3、u4-l4 详细拆解，这里只确认它们**存在且是引擎运行的最小必需数据**：

- [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) — 一份完整可用的拼音方案，是 console 示例（u1-l5）和大多数讲义实践的数据来源。

#### 4.4.4 代码实践

**实践目标**：亲手划清「公共头 / 内部头 / 捆绑第三方库」三条边界。

**操作步骤**：

1. 列出公共头：`ls src/*.h`，对照本节的 5 个文件清单。
2. 列出内部头根级文件：`ls src/rime/*.h`，确认它们定义的都是引擎内部类（engine、context、service 等），与公共 API 不同。
3. 列出捆绑第三方库：`ls include/`，确认 `darts.h`、`utf8.h`、`X11/` 不是 librime 写的（看 `include/COPYING.darts-clone` 的许可证）。
4. 打开 [CMakeLists.txt:L229-L232](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/CMakeLists.txt#L229-L232)，核对 `*_impl.h` 被排除安装的规则。

**需要观察的现象 / 预期结果**：

- `src/rime_api.h` 是公共头（安装），`src/rime/engine.h` 是内部头（默认不安装），二者目录不同、读者不同。
- `include/darts.h` 是 Prism 实现所依赖的第三方双数组 trie 库，不是 librime 的源码。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `rime_api_impl.h` 虽然放在公共头目录 `src/` 下，却不被安装？

**参考答案**：因为顶层 CMakeLists 用 `list(FILTER rime_public_header_files EXCLUDE REGEX .*_impl\.h$)` 把所有匹配 `*_impl.h` 的文件排除出安装列表（见 L230）。`_impl.h` 后缀是项目约定，表示「这是实现辅助头，不属于稳定公共 API」。

**练习 2**：一个只链接了 librime 动态库的前端程序，能直接 `#include <rime/engine.h>` 吗？为什么？

**参考答案**：默认不能。`engine.h` 是内部头，位于 `src/rime/`，默认不安装到系统 include 目录；前端只能 `#include <rime_api.h>` 走 C API。只有当 librime 以 `INSTALL_PRIVATE_HEADERS=ON` 构建并安装时，内部头才会被放到 `include/rime/` 下，此时写插件的开发者才能 `#include <rime/engine.h>`。这是 librime 区分「稳定 API」与「易变的内部实现」的边界设计。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这张「源码地图」小任务：

**任务**：为 librime 画一张**子目录职责图**，并标注「目录 → 模块组 → 依赖 → 代表文件」四列。

**建议步骤**：

1. 用只读命令采集事实：
   ```bash
   # 五大子目录的代表头文件
   ls src/rime/algo/*.h src/rime/config/*.h src/rime/dict/*.h src/rime/gear/*.h src/rime/lever/*.h | head -30
   ```
2. 打开 [src/CMakeLists.txt:L46-L58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/CMakeLists.txt#L46-L58)，记录每个模块组链接的第三方依赖。
3. 画一张图（手绘或用任何画图工具），结构如下：

   ```
                 ┌──────────── src/rime/（引擎内部实现）────────────┐
                 │  根级核心类：engine / context / service / schema ... │
                 │  四大组件基类：processor.h / segmentor.h /            │
                 │                translator.h / filter.h               │
                 ├──────────┬──────────┬──────────┬──────────┬─────────┤
                 │  algo    │  config  │   dict   │   gear   │  lever  │
                 │ 算法     │ 配置     │ 字典     │ 齿轮组件 │ 部署    │
                 │ calculus │config_   │dict_     │ speller  │deploy-  │
                 │ syllabif │types     │compiler  │translatr │ment_    │
                 │ encoder  │compiler  │prism     │filter    │tasks    │
                 │          │          │table     │          │         │
                 │ 依赖:    │ 依赖:    │ 依赖:    │ 依赖:    │ 依赖:   │
                 │ (并入    │ (并入    │ LevelDB  │ OpenCC   │ (无)    │
                 │  dict组) │  core组) │ marisa   │ ICU      │         │
                 └──────────┴──────────┴──────────┴──────────┴─────────┘
                          公共头 src/*.h（rime_api.h 等，安装给前端）
                          捆绑第三方 include/（darts.h / utf8.h）
                          数据集 data/minimal/（default.yaml + luna_pinyin 方案）
   ```

4. 在图上额外标注「**一次按键主要穿过哪些目录**」（提示：`gear/` 里的 Processor → Segmentor → Translator → Filter，其中 Translator 会调用 `algo/` 与 `dict/`），以及「**部署时主要穿过哪些目录**」（提示：`lever/` 调用 `dict/` 的 DictCompiler）。

**预期结果**：你会得到一张能长期挂在墙边的导航图。后续每读一篇讲义，都把新学的类（比如 u2-l4 的 Engine、u3-l1 的 Context）标注到这张图的对应目录里，地图会越来越丰满。**这是一个源码阅读型实践**，不需要编译运行。

## 6. 本讲小结

- librime 仓库分为构建元信息、源码本体（`src/` + `include/`）、数据（`data/`）、可执行工具（`tools/` + `sample/`）、扩展（`plugins/` + `test/`）几大类目录。
- `src/rime/` 内部按关注点分五大子目录：`algo/`（算法）、`config/`（配置）、`dict/`（字典）、`gear/`（流水线组件）、`lever/`（部署）；CMake 在安装私有头时正是遍历这五个目录。
- 「目录划分 ≈ 模块组 ≈ 依赖面」：`dict` 组依赖 LevelDB/marisa，`gear` 组依赖 OpenCC/ICU，`core` 组依赖 Boost/Glog/YamlCpp；默认情况下所有子目录合并编译成单一动态库 `rime`。
- 根级文件定义「骨架与契约」（Engine/Context/Service、四大组件基类），子目录（尤其 `gear/`）提供「具体实现」。
- 头文件分两层：公共头 `src/*.h`（`rime_api.h` 等，安装给前端）与内部头 `src/rime/*.h`（默认不安装，仅 `INSTALL_PRIVATE_HEADERS=ON` 时供插件用）；`include/` 是 vendoring 的第三方内联库（`darts.h` 等）。
- `src/rime/common.h` 是全局基础设施：标准库别名、智能指针别名（`the`/`an`/`of`/`weak`）、`As`/`Is`/`New`、Boost 信号槽、`rime::path`、日志开关——后续几乎每个 `.cc` 都会包含它。
- `data/minimal/` 提供最小可运行数据集（`default.yaml` + `luna_pinyin` 方案与词典 + 符号表 + 语料），是体验输入流程的基础。

## 7. 下一步学习建议

地图画好后，接下来该「进城」了：

- **下一步必读 u1-l4《C API 入口 rime_api.h》**：从公共头 `src/rime_api.h` 切入，看前端到底是怎么调用引擎的（`rime_get_api` → `RimeApi` → setup/session/input/config 四组方法）。这是从「地图」进入「使用方式」的桥梁。
- 之后 **u1-l5《实战：用 rime_api_console 体验输入流程》** 会用 `tools/rime_api_console.cc` 端到端跑一遍输入，把本讲提到的 `data/minimal/` 真正用起来。
- 想提前了解某个子目录的读者，可以直接跳读对应讲义：配置系统从 u4-l1 起、组件体系从 u5-l1 起、按键流水线从 u6-l1 起、字典系统从 u8-l1 起、部署从 u9-l1 起。但这些都需要先过 u2（核心运行时对象），建议按手册顺序推进。
- 建议把本讲「综合实践」产出的那张目录职责图保存下来，每学完一篇讲义就往图上补注，作为你个人的 librime 导航。
