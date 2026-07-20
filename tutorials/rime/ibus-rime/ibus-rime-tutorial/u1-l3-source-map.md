# 目录结构与源码地图

> 所属单元：U1 项目入门 · 第 3 讲
> 前置讲义：[u1-l2 依赖、构建与运行](u1-l2-build-and-run.md)

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 `ibus-rime` 仓库的整体目录树，并说出 `icons/`、`package/`、`cmake/`、`librime/`、`plum/` 这些目录分别装了什么。
2. 准确说出三个核心 C 源文件 `rime_main.c`、`rime_engine.c`、`rime_settings.c`（以及它们各自的头文件）的职责边界。
3. 看懂 `rime.xml.in` 这个 IBus 组件描述文件，并能解释 `engine` 节点下 `name`、`layout`、`symbol` 等字段的含义。
4. 建立「入口 / 引擎 / 设置」三层心智模型，为 U2（启动流程）与 U3（引擎对象）的源码精读打好地图。

本讲**不逐行讲算法**，只帮你建立「文件 ↔ 职责 ↔ 运行时角色」的对照关系。地图清楚了，后面走进任何一段源码都不会迷路。

## 2. 前置知识

在开始之前，确认你已经具备以下认知（来自 [u1-l1](u1-l1-project-positioning.md) 与 [u1-l2](u1-l2-build-and-run.md)）：

- **ibus-rime 是「薄前端」**：它本身不含输入法算法，真正的按键处理、查词、拼音转汉字都在 **librime**（一个 C++ 共享库）里。ibus-rime 只负责把 IBus 框架的 UI 原语翻译成对 librime `RimeApi` 的调用。
- **构建产物是单个可执行文件 `ibus-engine-rime`**：它被 IBus 守护进程通过 `--ibus` 参数启动，产物路径在构建期为 `build/`，安装期为 `/usr/lib/ibus-rime/`。
- **四类依赖**：`libibus-1.0`、`libnotify`、`librime`、`rime-data`，分别用 `pkg-config`、`find_package`、自定义 `FindRimeData.cmake` 发现（详见 u1-l2）。
- **配置模板 `rime_config.h.in`** 会在构建期被加工成 `build/rime_config.h`，注入 `IBUS_RIME_VERSION`、`IBUS_RIME_ICONS_DIR`、`IBUS_RIME_SHARED_DATA_DIR` 三个宏。

如果你对「IBus 组件 / 引擎」「GObject 类型系统」这些词还很陌生，不用慌——本讲会用最朴素的方式解释清楚。

> 名词速查
> - **IBus 组件（component）**：一组向 IBus 守护进程登记的输入法服务，用一份 XML 描述。一个组件可以包含一个或多个引擎（engine）。
> - **IBus 引擎（engine）**：真正处理按键、产出候选词的对象。ibus-rime 这个组件里只有一个引擎，名字叫 `rime`。
> - **GObject**：GNOME 生态的 C 语言对象系统。IBus 的引擎类型（`IBusEngine`）就是 GObject 子类；ibus-rime 要做的，就是再派生一个自己的子类 `IBusRimeEngine`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `rime_main.c` | **入口层**：程序 `main()`、IBus 总线、librime 生命周期 | 看清「入口」负责什么 |
| `rime_engine.c` | **引擎层**：`IBusRimeEngine` 类型、按键、UI 渲染 | 看清「引擎」是最大的文件 |
| `rime_engine.h` | 引擎类型对外声明 | 暴露 `IBUS_TYPE_RIME_ENGINE` 宏 |
| `rime_settings.c` | **设置层**：读取 `ibus_rime.yaml` | 看清「设置」最精简 |
| `rime_settings.h` | 设置结构对外声明 | 暴露 `g_ibus_rime_settings` 全局 |
| `rime.xml.in` | IBus 组件描述模板 | 理解组件如何被 IBus 发现 |
| `.gitmodules` | git 子模块定义 | 说明 `librime/`、`plum/` 的来源 |
| `CMakeLists.txt` | 构建脚本 | 说明三个 `.c` 如何被编进同一个可执行文件 |
| `ibus_rime.yaml` | 运行时配置 | 设置层的数据源 |

> 提示：本讲的永久链接全部基于当前 HEAD `ba8bfc3654c53d1723532907028ee6d59936b592`。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 仓库整体目录结构**——先看「房子」长什么样。
- **4.2 三个核心 C 源文件的职责划分**——再看每个「房间」住着谁。
- **4.3 IBus 组件描述文件 `rime.xml.in`**——IBus 怎么找到这栋房子。
- **4.4 入口 / 引擎 / 设置三层架构**——三个房间如何协作。

### 4.1 仓库整体目录结构

#### 4.1.1 概念说明

ibus-rime 是一个**体量很小的 C 项目**：根目录只有 3 个 `.c` 文件、3 个 `.h` 文件，外加构建脚本、图标、打包脚本和两个 git 子模块。理解它的第一步，不是读代码，而是先把目录树记在脑子里。

仓库可以粗分成五大块：

1. **源码区**：`rime_main.c` / `rime_engine.c` / `rime_settings.c` 及其头文件。
2. **资源区**：`icons/`（状态栏与组件图标）、`ibus_rime.yaml`（运行时配置）。
3. **构建区**：`CMakeLists.txt`、`Makefile`、`rime_config.h.in`、`rime.xml.in`、`cmake/FindRimeData.cmake`。
4. **打包区**：`package/` 下的三个 shell 脚本。
5. **子模块区**：`librime/`（核心引擎源码）、`plum/`（输入方案与词库管理）。

#### 4.1.2 核心流程

仓库根目录的目录树（**结构示意图，非命令输出**）如下：

```text
.
├── rime_main.c          # 【入口层】程序 main()、IBus 总线、librime 生命周期
├── rime_engine.c        # 【引擎层】IBusRimeEngine 类型、按键、UI 渲染（最大的文件）
├── rime_engine.h        #        引擎类型对外声明
├── rime_settings.c      # 【设置层】读取 ibus_rime.yaml
├── rime_settings.h      #        设置结构对外声明
├── rime.xml.in          # IBus 组件描述模板（构建期生成 rime.xml）
├── rime_config.h.in     # 编译期配置头模板（构建期生成 rime_config.h）
├── ibus_rime.yaml       # 运行时样式配置（被设置层读取）
├── CMakeLists.txt       # 构建脚本
├── Makefile             # 对 cmake 的薄封装
├── cmake/
│   └── FindRimeData.cmake   # 自定义的 rime-data 目录查找模块
├── icons/               # PNG 图标（组件图标 + 状态栏图标）
├── package/             # 打包脚本（make-package / make-binpkg-static / binpkg-install）
├── librime/             # git 子模块：Rime 核心引擎（C++ 库）
├── plum/                # git 子模块：输入方案与词库管理（产出 rime-data）
├── README.md
├── CHANGELOG.md
└── LICENSE
```

需要特别注意两点：

- **`librime/` 与 `plum/` 是 git 子模块**，仓库里只是「指针」，真正的内容在各自的 GitHub 仓库里。
- **图标 `icons/` 同时服务两处**：`rime.png` 是组件/引擎图标（出现在 `rime.xml.in` 与 IBus 偏好设置里），其余如 `zh.png`、`abc.png`、`disabled.png`、`reload.png`、`sync.png` 则是引擎运行时状态栏按钮的图标（在 `rime_engine.c` 里被引用）。

#### 4.1.3 源码精读

子模块定义在 [.gitmodules:1-6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/.gitmodules#L1-L6)：

```ini
[submodule "librime"]
        path = librime
        url = https://github.com/rime/librime.git
[submodule "plum"]
        path = plum
        url = https://github.com/rime/plum.git
```

这告诉我们 `librime/` 指向 [rime/librime](https://github.com/rime/librime)，`plum/` 指向 [rime/plum](https://github.com/rime/plum)。这正是 u1-l1 讲过的「核心引擎」与「方案/词库管家」的源码所在。

而构建脚本把根目录下**所有 `.c` 文件**收集起来，编进同一个可执行文件，见 [CMakeLists.txt:56-58](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L56-L58)：

```cmake
aux_source_directory(. IBUS_RIME_SRC)
add_executable(ibus-engine-rime ${IBUS_RIME_SRC})
target_link_libraries(ibus-engine-rime ${IBus_LIBRARIES} ${LIBNOTIFY_LIBRARIES} ${Rime_LIBRARIES} ${RIME_DEPS})
```

`aux_source_directory(. ...)` 会把当前目录（`.`）下全部 `.c` 文件塞进变量 `IBUS_RIME_SRC`，也就是 `rime_main.c`、`rime_engine.c`、`rime_settings.c` 三者最终被链接成**同一个二进制 `ibus-engine-rime`**。这也解释了为什么三个文件可以互相直接调用对方的全局变量与函数（例如 `rime_engine.c` 里 `extern RimeApi *rime_api;`）。

#### 4.1.4 代码实践

**实践目标**：亲手把目录树与磁盘对上号。

**操作步骤**：

1. 在仓库根目录执行 `ls -la`，对照上面的目录树逐项确认。
2. 执行 `ls icons/`，数一数共有几个 `.png`，分别猜猜它们会被用在哪里（组件图标 vs 状态栏按钮）。
3. 执行 `git submodule status`，观察 `librime/`、`plum/` 各自指向的 commit。

**需要观察的现象**：`librime/`、`plum/` 目录可能看起来是「空」的（只有 `.git` 文件），这是因为子模块尚未 `git submodule update --init`。

**预期结果**：你能在脑子里把 9 个根级文件 + 4 个目录（`icons/`、`cmake/`、`package/`、子模块）与它们的作用一一对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `librime/` 在仓库里是「空目录」，而不是一堆 `.cpp` 文件？

> **答案**：因为它是 git 子模块，仓库里只存了一条指向 `rime/librime` 某个 commit 的「指针」。需要 `git submodule update --init` 才会把真正的源码拉下来。

**练习 2**：`icons/` 里的 PNG 分属两类用途，试着根据文件名把它们分成两组。

> **答案**：组件/引擎图标一组：`rime.png`（出现在 `rime.xml.in`）。状态栏按钮图标一组：`zh.png`（中文模式）、`abc.png`（英文模式）、`disabled.png`（维护/禁用）、`reload.png`（部署）、`sync.png`（同步）。`keyboard.png`、`pen.png` 为预留/历史图标。

---

### 4.2 三个核心 C 源文件的职责划分

#### 4.2.1 概念说明

整个 ibus-rime 的 C 代码就三个文件，职责划分得非常干净：

| 文件 | 行数（约） | 一句话职责 |
| --- | --- | --- |
| `rime_main.c` | ~150 | 程序入口，拉起 IBus 总线、初始化/部署 librime、跑主循环 |
| `rime_engine.c` | ~610 | 定义 IBus 引擎类型，处理按键，把 librime 的状态渲染成 IBus 的 UI |
| `rime_settings.c` | ~90 | 读取 `ibus_rime.yaml`，维护一份全局样式设置 |

可以看到，**引擎文件占了绝大部分代码**——这很合理，因为「把 librime 的内部状态翻译成 IBus 能显示的预编辑文本、辅助文本、候选词表」是前端最繁重的活儿。入口和设置都只是「胶水」。

每个 `.c` 通常配一个 `.h` 对外暴露声明，但有个有趣的**不对称**：

- `rime_engine.h` 暴露类型宏 `IBUS_TYPE_RIME_ENGINE`。
- `rime_settings.h` 暴露全局结构 `g_ibus_rime_settings`。
- `rime_main.c` **没有对应头文件**：它对外暴露的 `ibus_rime_start` / `ibus_rime_stop` 是在 `rime_engine.c` 里用 `extern` 直接声明的（见 4.4.3）。

#### 4.2.2 核心流程

三个文件在运行时的「出场顺序」可以简化为：

```text
启动：
  rime_main.c 的 main()
    → 连接 IBus 总线、注册引擎类型（指向 rime_engine.c 的 IBUS_TYPE_RIME_ENGINE）
    → 初始化 librime、加载设置（rime_settings.c 的 ibus_rime_load_settings）
    → 进入 ibus_main() 主循环

运行中（每次有按键/焦点变化）：
  IBus 调用 rime_engine.c 里挂载的回调（process_key_event / focus_in / ...）
    → 调用 librime 处理按键
    → 读取 g_ibus_rime_settings 决定 UI 样式
    → 渲染预编辑/候选表

退出：
  rime_main.c 的 ibus_rime_stop() → librime finalize()
```

注意「注册引擎类型」这一步把入口层和引擎层缝合起来：`rime_main.c` 引用了 `rime_engine.h` 里声明的 `IBUS_TYPE_RIME_ENGINE`。

#### 4.2.3 源码精读

**(1) 入口文件** —— 文件首行注释就标明了身份，见 [rime_main.c:1](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L1)：

```c
// ibus-rime program entry
```

它持有贯穿全程序的全局 librime API 指针，见 [rime_main.c:23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L23)：

```c
RimeApi *rime_api = NULL;
```

这个 `rime_api` 会被 `rime_engine.c`、`rime_settings.c` 用 `extern` 引用，是三层之间的「共享总线」。

**(2) 引擎文件** —— 它定义了引擎对象本身。引擎实例持有哪些数据，见 [rime_engine.c:15-23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L15-L23)：

```c
struct _IBusRimeEngine {
  IBusEngine parent;

  /* members */
  RimeSessionId session_id;   // 当前 librime 会话
  RimeStatus status;          // 上一次的状态快照（用于去重）
  IBusLookupTable* table;     // 候选词表
  IBusPropList* props;        // 状态栏属性列表（中↔A、部署、同步）
};
```

这个结构告诉我们引擎层要管四样东西：会话、状态、候选表、状态栏按钮。本讲只要记住「引擎 = 这四样东西的管家」即可，细节留到 U3、U4。

引擎类型的注册由 GObject 宏一行完成，见 [rime_engine.c:67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L67)：

```c
G_DEFINE_TYPE (IBusRimeEngine, ibus_rime_engine, IBUS_TYPE_ENGINE)
```

这行宏会自动生成 `ibus_rime_engine_get_type()` 函数，而该函数又被头文件包装成宏，见 [rime_engine.h:6-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h#L6-L9)：

```c
#define IBUS_TYPE_RIME_ENGINE \
        (ibus_rime_engine_get_type())

GType ibus_rime_engine_get_type();
```

这个宏就是入口层 `ibus_factory_add_engine(..., IBUS_TYPE_RIME_ENGINE)` 传进去的「引擎类型标识」。

**(3) 设置文件** —— 它维护一个全局设置结构体，见 [rime_settings.c:24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L24)：

```c
struct IBusRimeSettings g_ibus_rime_settings;
```

它的字段（样式开关、光标类型、候选表方向、颜色方案指针）定义在头文件 [rime_settings.h:27-33](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L27-L33)，引擎层渲染 UI 时会读取它。整个加载入口是 [rime_settings.c:42-92](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L42-L92) 的 `ibus_rime_load_settings()`，细节留到 U5。

#### 4.2.4 代码实践

**实践目标**：用 `grep` 验证三个文件之间的依赖关系。

**操作步骤**：

1. 在仓库根目录执行（仅观察，不改代码）：
   - `grep -n "include \"rime_engine.h\"" rime_main.c` —— 确认入口层依赖引擎层声明。
   - `grep -n "include \"rime_settings.h\"" rime_main.c rime_engine.c` —— 确认入口层与引擎层都依赖设置层。
   - `grep -n "extern RimeApi" rime_engine.c rime_settings.c` —— 确认引擎层与设置层都「借用」入口层的全局 `rime_api`。

**需要观察的现象**：`rime_engine.c` 没有 `extern` 声明 `ibus_rime_load_settings`，而是直接 `#include "rime_settings.h"`；`rime_main.c` 反过来依赖 `rime_engine.h`。

**预期结果**：你会得出一条「设置层在最底、引擎层在中间、入口层在最上」的静态依赖链（注意：这是**静态 include 依赖**，与运行时调用方向不完全相同，见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rime_main.c` 没有对应的 `rime_main.h`？

> **答案**：因为入口层只在程序启动时被调用一次，不需要被其他编译单元「#include」。引擎层需要用到它的 `ibus_rime_start` / `ibus_rime_stop` 时，直接在 `rime_engine.c` 内用 `extern` 声明即可，不必专门建一个头文件。

**练习 2**：三个文件里哪一个是「最大的」，这反映了什么？

> **答案**：`rime_engine.c`（约 610 行）远大于 `rime_main.c`（约 150 行）和 `rime_settings.c`（约 90 行）。这说明「把 librime 的状态渲染成 IBus 的 UI」是这个前端最复杂的部分，后续 U3、U4 会花最多篇幅讲它。

---

### 4.3 IBus 组件描述文件 `rime.xml.in`

#### 4.3.1 概念说明

IBus 怎么知道系统里装了一个叫 Rime 的输入法？靠的是一份**组件描述文件** `rime.xml`。它告诉 IBus 守护进程：

- 这个组件叫什么（D-Bus 总线名）。
- 用什么命令启动它（可执行文件路径 + 参数）。
- 它提供哪些引擎（每个引擎的名字、语言、图标、布局等）。

仓库里的 `rime.xml.in` 是**模板**：里面的 `@CMAKE_INSTALL_FULL_LIBEXECDIR@`、`@CMAKE_INSTALL_FULL_DATADIR@` 是占位符，构建期由 CMake 的 `configure_file` 替换成真实安装路径，生成最终的 `build/rime.xml`，再安装到 `${datadir}/ibus/component/`。

> 与 u1-l2 的衔接：`configure_file` 加工 `rime.xml.in` 的逻辑在 [CMakeLists.txt:60-63](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L60-L63)，安装规则在 [CMakeLists.txt:65](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L65)。

#### 4.3.2 核心流程

`rime.xml.in` 分两级：**组件级**（`<component>`）与**引擎级**（`<engines><engine>`）。结构如下（**为讲解做了折叠，非完整文件**）：

```text
<component>
  ├── name        组件的 D-Bus 总线名（im.rime.Rime）
  ├── description 组件描述
  ├── exec        启动命令（ibus-engine-rime --ibus）
  ├── version / author / license / homepage / textdomain
  └── <engines>
        └── <engine>
              ├── name      引擎名（rime）       ← 工厂注册时的 key
              ├── language  语言（zh）
              ├── icon      引擎图标（rime.png）
              ├── layout    键盘布局（default）
              ├── longname  在 IBus 偏好设置里的显示名（Rime）
              ├── symbol    IBus 指示器里显示的符号（&#x37A2;）
              ├── rank      优先级（0）
              └── description
```

两个 `name` 字段最容易混淆，务必分清：

- **组件级 `<name>im.rime.Rime</name>`**：这是组件在 D-Bus 上的**总线名（bus name）**。
- **引擎级 `<name>rime</name>`**：这是引擎在组件内部的**引擎标识（engine id）**。

这两个名字**必须与代码里的字符串严格对应**，否则 IBus 找不到引擎。

#### 4.3.3 源码精读

完整的模板见 [rime.xml.in:1-26](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L1-L26)。其中引擎节点是核心，见 [rime.xml.in:13-24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L13-L24)：

```xml
<engine>
    <name>rime</name>
    <language>zh</language>
    <license>GPL</license>
    <author>...</author>
    <icon>@CMAKE_INSTALL_FULL_DATADIR@/ibus-rime/icons/rime.png</icon>
    <layout>default</layout>
    <longname>Rime</longname>
    <description>Rime Input Method Engine</description>
    <rank>0</rank>
    <symbol>&#x37A2;</symbol>
</engine>
```

各字段含义：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `name` | `rime` | 引擎标识。IBus 据此把按键事件路由给对应工厂。 |
| `language` | `zh` | 语言代码（中文）。 |
| `icon` | `.../icons/rime.png` | 引擎在 IBus 偏好设置里显示的图标。 |
| `layout` | `default` | 键盘布局，`default` 表示跟随系统默认布局。 |
| `longname` | `Rime` | 在 IBus 偏好设置里展示的人类可读名称。 |
| `symbol` | `&#x37A2;` | IBus 输入法指示器（如顶栏小图标旁）显示的符号字符，是 Unicode 码点 **U+37A2** 对应的一个 CJK 汉字。 |
| `rank` | `0` | 引擎排序权重，`0` 为默认。 |

启动命令在组件级，见 [rime.xml.in:6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L6)：

```xml
<exec>@CMAKE_INSTALL_FULL_LIBEXECDIR@/ibus-rime/ibus-engine-rime --ibus</exec>
```

`--ibus` 是 IBus 启动引擎进程时的**标准约定参数**，表示「以 IBus 引擎模式运行」。值得一提的是：本仓库的 `main()` 并未解析 `argv`（见 4.4.3），它直接进入 `rime_with_ibus()`，因此 `--ibus` 在这里更像是一个遵循 IBus 组件规范的标记位，而非代码会分支判断的开关。

**与代码的对应关系**（这是本模块最重要的结论）：

- 组件级总线名 `im.rime.Rime` ↔ [rime_main.c:110](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L110) 的 `ibus_bus_request_name(bus, "im.rime.Rime", 0)`。
- 引擎级引擎名 `rime` ↔ [rime_main.c:109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109) 的 `ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE)`。
- `<exec>` 的可执行文件 ↔ [CMakeLists.txt:57](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L57) 构建出的 `ibus-engine-rime`。

也就是说：**XML 声明「我叫什么」，代码里用相同的字符串去「认领」这个名字**。两边的字符串必须一字不差。

#### 4.3.4 代码实践

**实践目标**：验证「XML 名字 ↔ 代码字符串」的对应关系。

**操作步骤**：

1. 打开 `rime.xml.in`，找到组件级 `<name>` 与引擎级 `<name>` 两个值。
2. 在 `rime_main.c` 中搜索这两个字符串：
   - `grep -n "im.rime.Rime" rime_main.c`
   - `grep -n '"rime"' rime_main.c`
3. 对照搜索结果，确认它们分别出现在 `ibus_bus_request_name` 与 `ibus_factory_add_engine` 两行。

**需要观察的现象**：两个名字在代码里各出现一次，且分别绑定到「请求总线名」与「工厂注册引擎」两个动作上。

**预期结果**：你能讲清楚「如果把 `rime.xml.in` 里的引擎名改成 `rime2` 但代码不改，会发生什么」——IBus 会按 `rime2` 路由按键，但工厂只注册了 `rime`，于是引擎永远收不到事件。

#### 4.3.5 小练习与答案

**练习 1**：组件名 `im.rime.Rime` 和引擎名 `rime` 为什么不能写成同一个？

> **答案**：它们处于不同命名空间。组件名是 D-Bus 总线名（类似 `org.freedesktop.IBus` 这种反向域名格式），用于在系统总线上唯一标识一个服务进程；引擎名是组件内部用来区分「这个进程提供哪几种输入法」的标识。一个组件（一个总线名）下可以挂多个引擎。

**练习 2**：`<symbol>&#x37A2;</symbol>` 在用户界面上会出现在哪里？

> **答案**：它会作为 IBus 输入法指示器（例如 GNOME 顶栏输入法图标旁边）展示的短符号，让用户一眼看出当前是哪个输入法。它是 Unicode 码点 U+37A2 对应的单个 CJK 字符。

**练习 3**：`rime.xml.in` 里的 `@CMAKE_INSTALL_FULL_DATADIR@` 会在什么时候被替换？

> **答案**：构建期。CMake 的 `configure_file` 会把它替换成实际的安装前缀路径（如 `/usr/share`），生成 `build/rime.xml`，随后被 `install(FILES ...)` 安装到 `${datadir}/ibus/component/rime.xml`，供 ibus-daemon 扫描发现。

---

### 4.4 入口 / 引擎 / 设置三层架构

#### 4.4.1 概念说明

把 4.1～4.3 串起来，ibus-rime 其实是一个标准的**三层架构**：

- **入口层（`rime_main.c`）**：负责「程序级」事务——进程启动、信号处理、连接 IBus 总线、把引擎类型注册进工厂、初始化与部署 librime、进入主循环、退出时清理。它不关心按键和 UI。
- **引擎层（`rime_engine.c`）**：负责「交互级」事务——每个 IBus 引擎实例是一个 `IBusRimeEngine` 对象，它接收按键、维护会话、把 librime 的状态渲染成预编辑文本 / 辅助文本 / 候选词表 / 状态栏按钮。
- **设置层（`rime_settings.c`）**：负责「样式级」事务——读取 `ibus_rime.yaml`，把「是否内嵌预编辑、预编辑样式、光标类型、候选表方向、颜色方案」填进全局结构 `g_ibus_rime_settings`，供引擎层渲染时查询。

这种分层的好处是**关注点分离**：改启动流程不用动引擎逻辑；改 UI 样式只动设置层与引擎层的渲染分支；算法升级只换 librime，前端几乎不动。这正是 u1-l1 所说的「薄前端」得以成立的结构基础。

#### 4.4.2 核心流程

三层在运行时的协作图（**结构示意图**）：

```text
        ┌──────────────────────────────────────────┐
        │  ibus-daemon（IBus 守护进程）             │
        │  扫描 rime.xml → 按 --ibus 启动进程        │
        └───────────────────┬──────────────────────┘
                            │ D-Bus / IBusBus
        ┌───────────────────▼──────────────────────┐
        │  rime_main.c   【入口层】                  │
        │  main → rime_with_ibus → ibus_main         │
        │  职责：总线连接、工厂注册、librime 生命周期 │
        └──────┬──────────────────────┬─────────────┘
   注册类型     │                      │ 调用 rime_api
   IBUS_TYPE_  │                      │ （librime）
   RIME_ENGINE │                      │
        ┌──────▼─────────┐    ┌───────▼────────────────────────────┐
        │ rime_engine.c  │    │ librime（系统库 / 子模块）           │
        │ 【引擎层】      │    │ 真正的按键处理与查词算法            │
        │ 会话/按键/UI   │◄──►│ RimeApi                             │
        └──────┬─────────┘    └─────────────────────────────────────┘
   读取设置     │ g_ibus_rime_settings
        ┌──────▼─────────┐
        │ rime_settings.c│
        │ 【设置层】      │
        │ 解析           │
        │ ibus_rime.yaml │
        └────────────────┘
```

要点：

1. **入口层向引擎层「注册类型」**：入口把 `IBUS_TYPE_RIME_ENGINE`（来自引擎层）交给 IBus 工厂，之后每当用户切到 Rime 输入法，IBus 就会通过工厂**实例化**一个 `IBusRimeEngine` 对象。
2. **引擎层通过全局 `rime_api` 调用 librime**：`rime_api` 是入口层定义的全局指针，引擎层和设置层都用 `extern` 借用。
3. **引擎层从设置层读样式**：引擎渲染 UI 时查询 `g_ibus_rime_settings`，决定横向/纵向、内嵌与否、光标位置等。

#### 4.4.3 源码精读

**(1) 入口层的总装配** —— `rime_with_ibus()` 把三层串起来，见 [rime_main.c:94-136](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L94-L136)，其中注册引擎类型与请求总线名是关键两步：

```c
ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE);   // L109：注册引擎类型
if (!ibus_bus_request_name(bus, "im.rime.Rime", 0)) {                // L110：请求总线名
    g_error("error requesting bus name");
    exit(1);
}
```

随后入口层加载设置并进入主循环，见 [rime_main.c:125-129](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L125-L129)：

```c
gboolean full_check = FALSE;
ibus_rime_start(full_check);          // 初始化 + 部署 librime
ibus_rime_load_settings();            // 【设置层】加载 ibus_rime.yaml
ibus_main();                          // 进入 IBus 主循环（阻塞）
```

`main()` 本身极简，见 [rime_main.c:143-150](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L143-L150)：

```c
int main(gint argc, gchar** argv) {
  signal(SIGTERM, sigterm_cb);
  signal(SIGINT, sigterm_cb);

  rime_api = rime_get_api();          // 获取 librime 的 API 入口
  rime_with_ibus();
  return 0;
}
```

可以看到 `argc` / `argv` 并未被使用——这印证了 4.3.3 里关于 `--ibus` 只是约定标记、不被代码解析的结论。

**(2) 引擎层向入口层「借函数」** —— 引擎层在状态栏「部署」按钮里需要重启 librime，于是用 `extern` 声明入口层的两个函数，见 [rime_engine.c:546-557](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L546-L557)：

```c
static void ibus_rime_engine_property_activate(...) {
  extern void ibus_rime_start(gboolean full_check);
  extern void ibus_rime_stop();
  ...
  if (!strcmp("deploy", prop_name)) {
    ibus_rime_stop();
    ibus_rime_start(TRUE);
    ...
  }
}
```

这是「引擎层 → 入口层」的回调式调用，说明三层之间并非严格的单向依赖，而是以全局变量 `rime_api`、全局设置 `g_ibus_rime_settings`、`extern` 函数声明为纽带的**协作**关系。

**(3) 设置层向引擎层「供数据」** —— 设置层把读到的样式写进全局结构，见 [rime_settings.c:42-47](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L42-L47)：

```c
void ibus_rime_load_settings() {
  g_ibus_rime_settings = ibus_rime_settings_default;   // 先置默认值
  RimeConfig config = {0};
  if (!rime_api->config_open("ibus_rime", &config)) {  // 打开 ibus_rime.yaml
    g_error("error loading settings for ibus_rime");
    return;
  }
  ...
}
```

引擎层在渲染候选表方向时直接读取这个全局，见 [rime_engine.c:496-497](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L496-L497)：

```c
ibus_lookup_table_set_orientation(
    rime_engine->table, g_ibus_rime_settings.lookup_table_orientation);
```

这就是「配置驱动 UI」的完整闭环：`ibus_rime.yaml` → `rime_settings.c` → `g_ibus_rime_settings` → `rime_engine.c` 渲染分支。

#### 4.4.4 代码实践

**实践目标**：跟踪一条贯穿三层的调用链，把「地图」走一遍。

**操作步骤**（源码阅读型，无需运行）：

1. 从 [rime_main.c:143](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L143) 的 `main()` 出发，跳到 `rime_with_ibus()`（L94）。
2. 在 `rime_with_ibus()` 里依次标记：L109（注册引擎类型，连到引擎层）、L110（请求总线名，连到 `rime.xml.in`）、L126–L127（连到 librime 初始化与设置层）。
3. 想象用户按下一个键：IBus 会调用工厂注册时绑定的引擎类型所对应的 `process_key_event`，即 [rime_engine.c:509](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L509)，它最终会读取 `g_ibus_rime_settings`（设置层）来渲染。

**需要观察的现象**：你应该能在纸上画出「`main` → `rime_with_ibus` →（工厂/总线/设置/主循环）」与「按键 → `process_key_event` → librime → 渲染 → 读设置」两条线。

**预期结果**：你能用一句话回答「入口层、引擎层、设置层各自被谁调用、各自调用谁」。如果暂时说不清运行结果，标注「待本地验证」（例如在图形桌面里实际启动 ibus-rime 观察按键）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `ibus_rime_load_settings()` 从 `rime_with_ibus()` 里删掉，引擎还能工作吗？

> **答案**：能工作，但样式会退化。因为 `g_ibus_rime_settings` 不会被读取 `ibus_rime.yaml` 覆盖（不过它也未自动初始化为默认值——`ibus_rime_load_settings` 内部第一步才会赋默认值，见 [rime_settings.c:45](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L45)）。所以更准确的说法是：设置层不会被正确初始化，引擎渲染时读到的 `g_ibus_rime_settings` 内容不可预期，UI 样式可能错乱。

**练习 2**：引擎层和入口层之间有「双向」调用，请各举一例。

> **答案**：
> - 入口层 → 引擎层：入口层在工厂里注册 `IBUS_TYPE_RIME_ENGINE`（[rime_main.c:109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109)），随后由 IBus 实例化引擎对象。
> - 引擎层 → 入口层：引擎层在「部署」按钮回调里用 `extern` 调用入口层的 `ibus_rime_stop` / `ibus_rime_start`（[rime_engine.c:550-555](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L550-L555)）。

## 5. 综合实践

**任务**：制作一张「文件 → 职责 → 运行时角色」对照表，并标注 `rime.xml.in` 中 `engine` 的关键字段含义。

**操作步骤**：

1. **建表**：按下表格式填写（这里给出参考答案，建议你先自己填再对照）：

   | 文件 | 所属层 | 一句话职责 | 运行时角色 |
   | --- | --- | --- | --- |
   | `rime_main.c` | 入口层 | `main()`、IBus 总线、librime 生命周期、主循环 | 进程的「启动器」与「总装车间」 |
   | `rime_engine.c` | 引擎层 | `IBusRimeEngine` 类型、按键、UI 渲染 | 每个输入法实例的「大脑」 |
   | `rime_engine.h` | 引擎层 | 暴露 `IBUS_TYPE_RIME_ENGINE` 宏 | 给入口层注册用的「类型把手」 |
   | `rime_settings.c` | 设置层 | 读 `ibus_rime.yaml`，填 `g_ibus_rime_settings` | 样式的「数据源」 |
   | `rime_settings.h` | 设置层 | 暴露设置结构、枚举、全局变量声明 | 给引擎层查询用的「样式字典」 |
   | `rime.xml.in` | 声明层（构建期） | IBus 组件描述模板 | IBus 发现并启动本组件的「名片」 |

2. **标注 XML 字段**：在 `rime.xml.in` 的 `<engine>` 节点旁注上：
   - `name=rime` —— 引擎标识，与 `ibus_factory_add_engine(factory, "rime", ...)` 对应。
   - `layout=default` —— 跟随系统默认键盘布局。
   - `symbol=&#x37A2;` —— IBus 指示器里显示的符号字符（Unicode U+37A2）。
   - （补充）`longname=Rime` —— 偏好设置里的显示名；`icon=.../rime.png` —— 引擎图标。

3. **画连接线**：在表与标注之间画箭头，把「`rime.xml.in` 的 `name`」连到「`rime_main.c` 的注册代码」，确认两边字符串一致。

**验收标准**：

- 你能不看资料，指着目录树说出每个文件属于哪一层、干什么活。
- 你能解释「组件名 `im.rime.Rime`」与「引擎名 `rime`」的区别，并指出它们各自在代码里被「认领」的位置。
- 你能复述「配置驱动 UI」的闭环：`ibus_rime.yaml` → `rime_settings.c` → `g_ibus_rime_settings` → `rime_engine.c`。

> 若本地已配置好 IBus 与 librime，可进阶验证：用 `ibus read-config` 或在 IBus 偏好设置里确认「Rime」引擎是否出现、图标与名称是否与 `rime.xml.in` 一致。无法确认时标注「待本地验证」。

## 6. 本讲小结

- 仓库体量很小：根目录 3 个 `.c` + 3 个 `.h`，外加 `icons/`、`cmake/`、`package/` 三类资源/脚本目录，以及 `librime/`、`plum/` 两个 git 子模块。
- 三个核心 C 文件职责分明：`rime_main.c`（入口，约 150 行）、`rime_engine.c`（引擎，约 610 行，最重）、`rime_settings.c`（设置，约 90 行，最轻）。
- `rime.xml.in` 是 IBus 组件描述模板，分组件级与引擎级；**组件名 `im.rime.Rime`**（D-Bus 总线名）与**引擎名 `rime`**（引擎标识）是两个不同命名空间，必须与代码里的字符串严格一致。
- `aux_source_directory(.)` 把根目录所有 `.c` 编进同一个 `ibus-engine-rime` 可执行文件，三个文件因此能共享全局 `rime_api` 与 `extern` 函数。
- 整体是「入口 / 引擎 / 设置」三层架构，以全局 `rime_api`、全局 `g_ibus_rime_settings`、`extern` 声明为纽带协作；配置通过 `ibus_rime.yaml → rime_settings.c → g_ibus_rime_settings → rime_engine.c` 驱动 UI。
- `--ibus` 是 IBus 启动引擎进程的约定参数，本仓库 `main()` 不解析 `argv`，直接进入 `rime_with_ibus()`。

## 7. 下一步学习建议

有了这张地图，接下来的 U2「启动流程与 IBus 接入」会带你**走进入口层**：

- [u2-l1 main 入口与进程生命周期](../ibus-rime-tutorial/u2-l1-main-and-lifecycle.md)：从 `main()` 一路追到 `ibus_main()`，讲清信号处理与 `ibus_rime_start/stop`。
- [u2-l2 IBus 总线、工厂与引擎注册](../ibus-rime-tutorial/u2-l2-ibus-bus-factory.md)：精读 `rime_with_ibus()` 里总线连接、工厂创建、`add_engine`、`request_name` 的全过程——本讲提到的 [rime_main.c:109-110](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109-L110) 会在那里展开。
- [u2-l3 librime 初始化、部署与通知](../ibus-rime-tutorial/u2-l3-librime-init-deploy.md)：讲清 `setup` / `initialize` / `start_maintenance` / `deploy_config_file` 与 libnotify 通知。

建议阅读源码时：先把本讲的目录树和三层架构图放在手边，每读到一个函数就问自己「它属于哪一层、被谁调用」，这样不容易在 610 行的 `rime_engine.c` 里迷路。
