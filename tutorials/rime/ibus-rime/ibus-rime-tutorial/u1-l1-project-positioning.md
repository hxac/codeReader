# 项目定位与 Rime 生态

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在还没有读懂任何一行 C 代码之前，先搞清楚一个最根本的问题：

**ibus-rime 到底是个什么东西？它在整个 Rime 输入法体系里扮演什么角色？**

读完本讲，你应该能够：

- 说清楚 ibus-rime、librime、plum 三者各自的职责，以及谁依赖谁。
- 理解「输入法引擎」和「桌面输入法框架（IBus）」是两个不同的概念，并说明为什么要把它们分开。
- 看懂 ibus-rime 的运行平台、许可证，以及它的外部依赖都从哪里来。
- 用一张依赖/调用方向图，把 ibus-rime 在 Rime 生态中的「薄前端」定位表达出来。

本讲几乎不涉及算法和实现细节，重点是把「生态地图」画对。地图画对了，后面所有讲义（启动流程、引擎对象、UI 渲染、配置系统）才有坐标。

## 2. 前置知识

本讲是 beginner 级别，不要求你会写 C，也不要求你用过 Rime。只需要理解以下几个生活化的概念：

- **输入法（Input Method, IM）**：当我们用键盘打字时，操作系统并不会自动把按键变成「中文字」。输入法就是那个夹在键盘和应用程序之间、负责把按键序列转换成文字的程序。
- **输入法引擎（Engine）**：输入法里真正负责「把按键变成候选词、根据词库做转换」的算法核心。Rime 项目的核心引擎叫 librime。
- **桌面输入法框架（Framework）**：操作系统桌面环境提供的一套基础设施，负责管理「当前激活了哪个输入法」「把候选词面板画在屏幕哪里」「把文本提交给哪个正在编辑的应用窗口」。在 Linux 桌面上，最常见的两个框架是 IBus 和 Fcitx。
- **前端（Frontend）**：把「引擎」接入到「框架」的那一层胶水代码。ibus-rime 就是 librime 这个引擎在 IBus 这个框架下的前端。

一个形象的比喻：

> librime 是「发动机」，IBus 是「整车平台和方向盘」，ibus-rime 是把发动机装到车上的「变速箱和传动轴」。

如果你能接受这个比喻，本讲后面所有的源码引用都会变得很自然。

## 3. 本讲源码地图

本讲只读项目里最「外围」的几个说明性文件，它们体量都很小，却几乎包含了全部定位信息：

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `README.md` | 项目自述文件，一句话定位 + 依赖清单 | 确认项目身份、许可证、构建/运行依赖 |
| `.gitmodules` | Git 子模块声明，记录 librime 与 plum 两个外部仓库 | 理解 Rime 生态里三个仓库的代码组织方式 |
| `CHANGELOG.md` | 版本变更日志 | 通过历史变更（迁移到 librime 1.x、迁移到 plum）佐证「前端/核心分离」的设计 |
| `rime.xml.in` | IBus 组件描述文件模板 | 直观展示 ibus-rime 如何「自我介绍」给 IBus 框架 |

> 说明：`rime.xml.in` 不在本讲规格的 `source_files` 里，但它只有 26 行，且最能直观体现「ibus-rime 向 IBus 注册自己」这件事，所以本讲会少量引用它作为辅助理解，不深入。

## 4. 核心概念与源码讲解

### 4.1 Rime 生态全景：librime、plum 与 ibus-rime 的分工

#### 4.1.1 概念说明

很多人第一次听到「Rime」会以为它是一个输入法软件，装上就能打字。其实 Rime 是一个**项目族**，由若干个分工明确的仓库组成。和本讲最相关的有三个：

- **librime**：Rime 的**核心引擎**，一个 C++ 共享库。它知道怎么处理按键、怎么查词库、怎么做拼音到汉字的转换，但它**完全不知道**屏幕长什么样、也不关心你用的是 IBus 还是 Fcitx。
- **plum**：Rime 的**配置与词库管理工具**（早期叫 brise）。它负责下载、安装、更新输入方案（schema）和词库数据。这些数据文件统称为 **rime-data**。
- **ibus-rime**：本项目。它是把 librime 接入 IBus 框架的**前端适配器**，是一份 C 源码，编译后会得到一个叫 `ibus-engine-rime` 的可执行文件。

一句话区分：**librime 是大脑，plum 是图书馆管理员，ibus-rime 是 IBus 桌面上的那副「外壳和按键面板」。**

#### 4.1.2 核心流程

从「用户敲键盘」到「汉字出现在屏幕上」的简化流程：

1. 用户在某个应用窗口（比如浏览器地址栏）里敲下一个键。
2. **IBus 框架**捕获这次按键，发现当前激活的输入法是 Rime，于是把按键事件转发给它加载的 `ibus-engine-rime` 进程。
3. **ibus-rime**（前端）收到按键，把它翻译成 librime 能理解的形式，调用 **librime**（核心）的接口。
4. librime 根据当前输入方案和词库算出候选词，把结果返回给 ibus-rime。
5. ibus-rime 把候选词、预编辑文本翻译回 IBus 的 UI 原语，让 IBus 框架画出候选面板。
6. 用户选词后，ibus-rime 再通过 IBus 把最终文本「提交」给应用窗口。

可以看到：算法在第 4 步（librime 内部），界面在第 5、6 步（通过 IBus），而 ibus-rime 一直在做第 3、5、6 步的**翻译与转发**。这就是「薄前端」的含义。

#### 4.1.3 源码精读

先看 `README.md` 的开头，这一句话就是整个项目的身份证：

[README.md:L1-L9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L1-L9) —— 第 1–3 行给出项目名与一句话定位「Rime Input Method Engine for Linux/IBus」，第 5 行指向项目主页 rime.im，第 9 行声明许可证为 GPLv3-or-later。

> 注意定位里两个关键词：「for Linux/IBus」说明它是面向 IBus 框架的；而它只是「Engine for ...」这个框架，真正的算法引擎在 librime 里。

再看 ibus-rime 是怎么把 librime 和 plum 这两个外部仓库「挂」进来的。打开 `.gitmodules`：

[.gitmodules:L1-L6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/.gitmodules#L1-L6) —— 这里用 Git submodule 机制声明了两个子模块：第 1–3 行是 `librime`（核心引擎源码），第 4–6 行是 `plum`（方案/词库管理工具），它们各自指向 rime 组织下的独立仓库。

把这两段连起来读，就能得到一条很重要的结论：

- `librime` 的源码**不在本仓库里**，它是作为子模块引用进来的（主要用于静态打包，见 U6）。
- `plum` 同理。但运行时，ibus-rime 并不直接需要 plum 的源码，而是需要 **plum 产出的 rime-data 数据文件**。

这一点在依赖清单里写得很清楚。看 `README.md` 的运行时依赖：

[README.md:L24-L30](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L24-L30) —— 第 26–28 行是程序运行所必需的库（ibus、librime、libibus-1.0、libnotify），第 30 行特别注明 `rime-data (provided by plum)`，即数据文件由 plum 提供。

最后用 `CHANGELOG.md` 里的两次「迁移」作为旁证，确认这三者本来是各自独立的组件：

[CHANGELOG.md:L36-L44](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CHANGELOG.md#L36-L44) —— 1.5.0 版本的「Features」里记录了 `submodules: migrate to rime/plum`，说明项目历史上曾用 brise，后来统一迁移到 rime/plum 体系，印证了「核心/数据/前端」是分开维护的。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（本讲不编译代码，重点是理清关系）。

1. **实践目标**：亲手验证「librime 与 plum 是独立仓库，通过子模块引入」。
2. **操作步骤**：
   - 打开本仓库根目录的 `.gitmodules` 文件。
   - 记录下 `librime` 和 `plum` 各自的 `url` 字段。
   - 在浏览器里分别打开这两个 url，确认它们确实是 rime 组织下的独立仓库。
3. **需要观察的现象**：两个 url 指向 `github.com/rime/librime.git` 和 `github.com/rime/plum.git`，而不是 ibus-rime 仓库内部的子目录。
4. **预期结果**：你能向别人解释「如果你想看 librime 的算法实现，要去 librime 仓库，而不是本仓库」。
5. 运行结果：待本地验证（如果你本地 `git submodule update --init` 过，会看到 `librime/`、`plum/` 目录被填充；否则这两个目录可能是空的，这是正常的）。

#### 4.1.5 小练习与答案

**练习 1**：如果某天 librime 升级了拼音转换算法，ibus-rime 这边的代码需要改吗？

> **参考答案**：通常不需要。因为 librime 对外提供的是稳定的 RimeApi 接口，ibus-rime 只调用这层接口。算法升级发生在 librime 内部，对前端透明。这也是「前端/核心分离」最大的好处之一。

**练习 2**：为什么 `README.md` 把 plum 列为 build 依赖（第 22 行 `plum (submodule)`），而运行时只需要 `rime-data (provided by plum)`？

> **参考答案**：构建时（尤其静态打包）需要 plum 的源码来生成/打包数据；而普通运行时，用户系统里只要有 plum 安装好的 rime-data 数据文件即可，不需要 plum 的源码或工具本身。

---

### 4.2 IBus 输入法框架：输入法引擎与桌面框架的关系

#### 4.2.1 概念说明

很多初学者会把「输入法引擎」和「输入法框架」混为一谈，这里必须分清：

- **输入法框架（IBus）**：桌面环境层面的基础设施。它常驻后台（`ibus-daemon`），负责和各个图形应用打交道，管理「现在该用哪个输入法」，并负责绘制候选词面板、状态栏图标。IBus 本身不包含任何中文拼音算法。
- **输入法引擎（engine）**：被框架加载的具体输入法实现。IBus 允许同时安装多个引擎（拼音、五笔、英文……），用户在状态栏里切换。Rime 就是这些「引擎」中的一个。

IBus 的扩展机制是「组件（component）」：每个第三方输入法以一个独立进程的形式存在，通过 D-Bus 总线和 IBus 守护进程通信。ibus-rime 编译出来的 `ibus-engine-rime` 就是这样一个组件进程。

#### 4.2.2 核心流程

IBus 加载一个输入法组件的典型流程：

1. IBus 守护进程启动时，扫描 `~/.local/share/ibus/component/` 和系统级目录下的组件描述文件（`.xml`）。
2. 读到 `rime.xml`，发现里面声明了一个组件 `im.rime.Rime`，它的可执行文件是 `ibus-engine-rime --ibus`。
3. 当用户切换到 Rime 输入法时，IBus 按描述文件里的 `exec` 字段拉起 `ibus-engine-rime` 进程。
4. 该进程连上 IBus 的 D-Bus 总线，注册自己提供的一个引擎类型（叫 `rime`），此后按键事件就通过总线送给这个进程。
5. 进程内部再把按键交给 librime 处理（这正是 4.1 讲的流程）。

#### 4.2.3 源码精读

先看 `README.md` 里和 IBus 相关的依赖：

[README.md:L15-L22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L15-L22) —— 构建依赖里第 20 行 `libibus-1.0 (development package)` 就是 IBus 提供的 C 开发包，ibus-rime 的代码靠它的头文件和库来编译；运行时依赖里第 26 行 `ibus` 则是 IBus 守护进程本身。

然后看 ibus-rime 是怎么「自我介绍」给 IBus 的。打开 `rime.xml.in`（这是模板，构建时会被替换成 `rime.xml`）：

[rime.xml.in:L3-L11](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L3-L11) —— `<component>` 块就是给 IBus 看的名片：第 4 行 `<name>im.rime.Rime</name>` 是组件的唯一标识，第 6 行 `<exec>...ibus-engine-rime --ibus</exec>` 告诉 IBus「要启动我，就执行这个命令」。注意 `--ibus` 这个参数——它正是 `rime_main.c` 里 `main()` 用来区分「作为 IBus 引擎运行」的标志（U2 会讲）。

[rime.xml.in:L13-L24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L13-L24) —— `<engines>` 块里声明了这个组件实际提供的一个输入法引擎：第 14 行 `<name>rime</name>`，第 19 行 `<layout>default</layout>` 表示键盘布局，第 23 行 `<symbol>` 是状态栏上显示的汉字图标。这一段就是 IBus 状态栏里那个「中/Rime」条目的来源。

`CHANGELOG.md` 里也能看到这条「名片」的演变：

[CHANGELOG.md:L60-L69](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CHANGELOG.md#L60-L69) —— 1.4.0 版本里记录 `rime.xml: update ibus component name to im.rime.Rime`，说明组件名经历过规范化，这个 `im.rime.Rime` 正是上面 `rime.xml.in` 第 4 行的取值。

#### 4.2.4 代码实践

1. **实践目标**：建立「IBus 通过 xml 描述文件发现并拉起引擎进程」的直观认识。
2. **操作步骤**：
   - 打开 `rime.xml.in`，找到 `<component>` 的 `name` 和 `exec` 两个字段。
   - 再打开 `README.md` 的依赖清单，找到 `libibus-1.0` 和 `ibus` 两项。
   - 在脑子里（或纸上）连一条线：IBus 守护进程 → 读 `rime.xml` → 执行 `ibus-engine-rime --ibus` → 进程依赖 `libibus-1.0` 与 IBus 通信。
3. **需要观察的现象**：`exec` 字段里的可执行文件名 `ibus-engine-rime`，和 README 里描述的项目产物是一致的；它的命名也暗示了「这是一个 IBus engine」。
4. **预期结果**：你能解释「为什么卸载了 ibus 守护进程，ibus-rime 也就没法用了」——因为没有了框架，前端进程没人调度。
5. 运行结果：待本地验证（如果你装了 ibus，可以执行 `ibus list-engine` 看是否能列出 rime）。

#### 4.2.5 小练习与答案

**练习 1**：`rime.xml.in` 里组件名是 `im.rime.Rime`，引擎名是 `rime`，这俩有什么区别？

> **参考答案**：组件名（component name）标识「这个后台进程/可执行文件」，一个组件可以提供**多个**引擎；引擎名（engine name）标识「用户能切换的那个具体输入法」。本项目里一个组件只挂了一个引擎，但概念上它们是两层。

**练习 2**：为什么 ibus-rime 运行时既依赖 `ibus` 又依赖 `libibus-1.0`？

> **参考答案**：`ibus` 是**守护进程/运行时环境**，负责调度和 UI；`libibus-1.0` 是**客户端开发库**，`ibus-engine-rime` 进程靠它提供的 API 连上 D-Bus 总线、和守护进程通信。两者一个是「服务方基础设施」，一个是「自己要链接的库」。

---

### 4.3 前端与核心的分层：为什么 ibus-rime 是「薄前端」

#### 4.3.1 概念说明

把 4.1 和 4.2 合在一起看，会浮现出一个清晰的分层结构：

```
        ┌──────────────────────────────────────────────┐
        │  图形应用（浏览器、编辑器……）                  │
        └──────────────────────────────────────────────┘
                            ▲ 提交文本 / ▼ 按键事件
        ┌──────────────────────────────────────────────┐
        │  IBus 框架（ibus-daemon + libibus-1.0）       │  ← 桌面框架
        └──────────────────────────────────────────────┘
                            ▲ 引擎接口 / ▼ 转发按键
        ┌──────────────────────────────────────────────┐
        │  ibus-rime（ibus-engine-rime，前端/适配层）   │  ← 本项目
        └──────────────────────────────────────────────┘
                            ▲ RimeApi / ▼ 调用
        ┌──────────────────────────────────────────────┐
        │  librime（核心引擎：算法、词库、方案）        │  ← 核心
        └──────────────────────────────────────────────┘
                            ▲ 读取
        ┌──────────────────────────────────────────────┐
        │  rime-data（方案与词库，由 plum 提供）        │  ← 数据
        └──────────────────────────────────────────────┘
```

ibus-rime 处在「IBus 框架」和「librime 核心」之间，它的代码量不大，核心职责只有三件：

1. **把 IBus 的原语翻译成 librime 的调用**（按键事件 → `process_key`）。
2. **把 librime 的结果翻译回 IBus 的原语**（候选词、预编辑文本 → IBusLookupTable、IBusText）。
3. **管理生命周期与配置**（启动/退出、部署、加载 `ibus_rime.yaml`）。

正因为它「不含算法、只做翻译」，所以被称为**薄前端（thin frontend）**。

#### 4.3.2 核心流程

「薄前端」这个定位决定了整个项目的代码组织。可以用下面这条「依赖倒置」的链路来理解为什么分层是有意的：

1. librime 对外暴露一套相对稳定的 **RimeApi** C 接口（用 `rime_get_api()` 获取一个函数指针结构体）。
2. ibus-rime 只依赖这层接口，不依赖 librime 的内部实现。
3. 因此 librime 可以独立升级、独立替换实现，只要接口不变，ibus-rime 就不用改。
4. 同理，如果哪天出现一个新的桌面框架（比如某个新出的 Wayland 原生 IM 框架），只需要写一个新的「薄前端」，核心 librime 完全复用。

这正是 Rime 生态的架构精髓：**一处实现核心，多处编写前端**。今天已经存在 ibus-rime（IBus）、fcitx-rime（Fcitx）、squirrel（macOS）、weasel（Windows）等多个前端，它们共享同一个 librime。

#### 4.3.3 源码精读

最能体现「薄前端」定位的，是 `README.md` 第 3 行那句高度浓缩的话，以及它对应的依赖切分：

[README.md:L1-L3](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L1-L3) —— 「Rime Input Method Engine **for Linux/IBus**」。注意它不说自己是「the engine」，而是「engine **for** IBus」——核心引擎是 librime，它只是面向 IBus 的那一层。

把构建依赖整体看一遍，会发现依赖被天然分成了三组，正好对应分层：

[README.md:L15-L22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L15-L22) ——
- 「对接框架」组：`libibus-1.0`（第 20 行）—— 用来和 IBus 说话。
- 「对接核心」组：`librime>=1.0`（第 19 行）—— 用来调用 Rime 引擎。
- 「翻译/通知」组：`libnotify`（第 21 行）—— 用来在部署时弹桌面通知。

这三组依赖，恰好就是 ibus-rime 作为「中间翻译层」需要的三类外部能力。它自己不实现任何一组，全部是「调用别人」。

`CHANGELOG.md` 里有一条关键迁移，直接点明了「核心 API 边界」的存在：

[CHANGELOG.md:L60-L69](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CHANGELOG.md#L60-L69) —— 1.4.0 记录了 `migrate to librime 1.x API`。这次迁移说明 librime 的对外 API 是一条明确、有版本的边界；ibus-rime 曾经跟随这条边界做过一次较大的适配，这恰恰证明了「前端依赖的是接口，不是实现」。

#### 4.3.4 代码实践

1. **实践目标**：把本项目的依赖「归类」到分层架构里，验证「薄前端」的说法。
2. **操作步骤**：
   - 列出 `README.md` 第 15–22 行的全部构建依赖。
   - 建一张三列表格，表头分别是「对接 IBus 框架」「对接 librime 核心」「辅助/数据」。
   - 把每个依赖填进对应列：`libibus-1.0`→框架列，`librime`→核心列，`libnotify`→辅助列，`plum`/`rime-data`→数据列，`cmake`/`pkg-config`→构建工具列（单独画一行）。
3. **需要观察的现象**：你会发现没有任何一项依赖是「拼音算法库」或「词库查询库」——因为这些都在 librime 内部，对前端不可见。
4. **预期结果**：表格清晰显示「ibus-rime 的依赖全是胶水所需的接口库，没有核心算法库」，从而印证「薄前端」。
5. 运行结果：待本地验证（你可以打开 `CMakeLists.txt` 搜索 `pkg_check_modules`，确认实际链接的库和这张表一致，U1-L2 会详细讲）。

#### 4.3.5 小练习与答案

**练习 1**：假设现在要做一个「rime-win」前端，让 librime 跑在某个新的 Windows 输入法框架上，本项目里哪些代码可以完全复用？

> **参考答案**：几乎**不能直接复用** ibus-rime 的源码，因为它和 IBus 的 GObject、IBusEngine 虚函数、`rime.xml` 紧密绑定。能复用的是**它调用 librime 的那套思路**（RimeApi、session、process_key），以及配置加载的理念。这反过来说明：前端是「框架相关」的，核心是「框架无关」的。

**练习 2**：为什么 ibus-rime 的源码文件那么少（核心就 `rime_main.c`、`rime_engine.c`、`rime_settings.c` 三个），却可以实现一个功能完整的中文输入法？

> **参考答案**：因为「功能完整」的绝大部分工作量（拼音切分、词频、用户词学习、方案切换、繁简转换……）都在 librime 里。ibus-rime 只负责「接按键、传结果、画界面」，所以代码量天然不大——这正是「薄前端」的直接体现。

## 5. 综合实践

本讲的综合实践，是把规格里要求的那张「生态依赖图」亲手画出来。请准备纸笔或任意画图工具，完成下面的任务。

**任务：绘制 Rime 生态依赖与调用方向图**

1. 画出 4 个节点：`IBus 框架`、`ibus-rime（本项目）`、`librime（核心引擎）`、`rime-data（由 plum 提供）`。如果你愿意，可以把 `plum` 作为第 5 个节点单独画出来。
2. 用**实线箭头**表示「调用/数据流向」，并在箭头上标注发生了什么，例如：
   - `IBus 框架 → ibus-rime`：转发按键事件、要求绘制候选面板。
   - `ibus-rime → librime`：调用 RimeApi 处理按键、获取候选。
   - `librime → rime-data`：读取输入方案与词库。
   - `plum → rime-data`：安装/更新方案与词库。
3. 用**虚线箭头**或不同颜色表示「依赖关系」（编译/运行时依赖），例如 `ibus-rime` 依赖 `libibus-1.0`、`librime`、`libnotify`。
4. 在图上用文字标注三件事：
   - 谁是**前端**？（答：ibus-rime）
   - 谁是**核心引擎**？（答：librime）
   - 谁是**桌面框架**？（答：IBus）
5. 在图旁写一句话总结：**「ibus-rime 是 librime 在 IBus 下的薄前端，自己不含算法，只做框架与核心之间的翻译。」**

**自我检查**：如果你画出的图里，箭头从 IBus 出发，经过 ibus-rime，再到 librime，最后落到 rime-data，方向是单向、无环的，那么你的理解就是对的。

> 本实践为源码阅读/画图型实践，不涉及编译运行，无需本地验证命令执行结果；但建议你对照 `README.md` 的依赖清单与 `.gitmodules` 的子模块声明逐条核对图中节点，确保没有遗漏。

## 6. 本讲小结

- Rime 是一个**项目族**：librime 是核心引擎、plum 提供方案与词库数据（rime-data）、ibus-rime 是 Linux/IBus 下的前端适配器。
- **输入法引擎**（算算法的 librime）和**输入法框架**（管界面与调度的 IBus）是两层不同的东西，ibus-rime 负责把前者接入后者。
- ibus-rime 编译产物是 `ibus-engine-rime`，通过 `rime.xml` 向 IBus 注册自己，进程启动参数带 `--ibus`。
- 项目依赖天然分成「对接框架（libibus-1.0）」「对接核心（librime）」「辅助（libnotify）」三组，这正是「薄前端」的指纹。
- librime 通过 **RimeApi** 这条有版本的稳定边界对外提供服务（CHANGELOG 中 1.4.0 的 `migrate to librime 1.x API` 即佐证），核心可独立升级而不影响前端。
- ibus-rime 代码量小、不含拼音算法，所有「重活」都在 librime 与 rime-data 里——这正是它能成为多个 Rime 前端之一的原因。

## 7. 下一步学习建议

定位清楚了，下一步就该看「它是怎么跑起来的」。建议按以下顺序继续：

- **U1-L2（依赖、构建与运行）**：动手用 CMake + Makefile 把项目构建出来，搞清楚 `rime_config.h` 里那些路径宏是怎么生成的，并亲眼看到 `ibus-engine-rime` 这个可执行文件被产出的位置。
- **U1-L3（目录结构与源码地图）**：建立 `rime_main.c` / `rime_engine.c` / `rime_settings.c` 三个核心 C 文件的职责对照表，为进入 U2 做准备。
- **课外延伸（可选）**：如果你对 librime 本身感兴趣，可以打开 `.gitmodules` 里指向的 [librime 仓库](https://github.com/rime/librime)，浏览它的 `rime_api.h`，提前感受一下 ibus-rime 所依赖的那条「稳定 API 边界」长什么样。
