# 项目定位：Squirrel 是什么

## 1. 本讲目标

本讲是整套 Squirrel（鼠鬚管）学习手册的第一篇。读完后你应当能够：

- 说清楚 **Squirrel 在 Rime 生态里扮演什么角色**——它到底是「输入法引擎」还是「输入法前端」。
- 识别 Squirrel 依赖的三个关键外部组件（librime、plum、Sparkle），并各用一句话说明它们的作用。
- 知道项目的 **授权方式（GPL v3）** 和 **运行平台要求（macOS 13.0+）**。
- 理解 Rime 如何通过「共享引擎 + 各平台前端」实现跨平台。

这是后续所有讲义的认知地基：只有先分清「前端」与「引擎」，后面读源码时才不会把 Squirrel 的 Swift 代码和 librime 的 C 代码混为一谈。

## 2. 前置知识

在开始之前，用最通俗的方式建立几个概念。如果你已经熟悉，可以跳过本节。

- **输入法（Input Method, IM）**：当你用键盘敲 `n-i-h-a-o`，屏幕上却出现「你好」时，中间那段「把按键翻译成汉字」的程序就是输入法。macOS 自带拼音、英文等输入法，而 Squirrel 是一个可以替换它们的第三方输入法。
- **候选词（candidate）**：输入 `ni` 后弹出的「你/泥/拟/逆……」列表，每一项就是一个候选词。
- **输入方案（schema）**：一套「怎么把按键映射成汉字」的规则集合，例如「朙月拼音」「双拼」「注音」。一个输入法可以同时装很多个方案，按快捷键切换。
- **前端（frontend）vs 引擎（engine）**：
  - **引擎**：真正干「按键 → 候选词」这套核心逻辑的程序。它懂拼音、懂字频、懂词库。
  - **前端**：负责跟操作系统打交道的那一层——接收键盘事件、画出候选词面板、把选中的字交给当前正在用的 App。前端本身**不做**中文转换，它把按键转手交给引擎，再把引擎的结果画出来。
- **InputMethodKit（IMK）**：macOS 提供给输入法开发者的官方框架。Squirrel 就是用 IMK 跟 macOS 对接的。
- **git submodule（子模块）**：一个 git 仓库里嵌套引用的另一个 git 仓库。Squirrel 把 librime、plum、Sparkle 三个独立项目以子模块形式拉进自己的仓库里一起构建。

记住一句话：**Squirrel 是「壳」，引擎是「芯」。** 后面的所有源码讲解都会围绕这个分工展开。

## 3. 本讲源码地图

本讲只读两个项目文件，它们都是文档而非代码，但信息量极大：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「身份证」：名称、授权、平台、致谢、引用的开源软件清单。 |
| `SKILL.md` | 面向开发者的架构速查手册：仓库地图、启动流程、各模块职责、编程约定。 |

另外会顺带引用一个配置文件 `.gitmodules`，用来确认三个外部组件的来源。

> 说明：本讲的「源码」主要是这两份文档的原文。文档也是项目的一部分，引用时同样给出永久链接和行号，便于你对照阅读。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：项目定位与名称由来、Rime 引擎生态、授权与跨平台关系。

### 4.1 项目定位与名称由来

#### 4.1.1 概念说明

**鼠鬚管**（Squirrel）是 [Rime 输入法](https://rime.im) 在 macOS 上的官方发行版，也就是「macOS 版的 Rime」。

「鼠鬚管」这个名字来自一种毛笔——**鼠须笔**，用鼠（老鼠）的胡须制成的毛笔。README 顶部引用了欧阳修的诗：

> 鼠鬚管
> 爲物雖微情不淺
> 新詩醉墨時一揮

借毛笔「挥毫写字」的意象，呼应「输入法是用来写字的工具」。而英文代号 **Squirrel（松鼠）** 则是对「鼠」字的俏皮转译——松鼠也是一种鼠。所以你会看到：中文名强调「文房工具」，英文名强调「鼠」。

关键定位只有一句话：**Squirrel 是一个 macOS 输入法前端，它本身不做中文转换，真正的转换工作交给引擎 librime 完成。**

#### 4.1.2 核心流程：如何判断「它是前端还是引擎」

判断一个输入法项目是前端还是引擎，可以沿下面的判断流程走：

```text
这个二进制程序自己实现了「按键→候选词」的转换逻辑吗？
├── 是 ──→ 它是「引擎」
└── 否 ──→ 它把按键交给别人处理吗？
            ├── 是 ──→ 它是「前端」，被交给的那个才是引擎
            └── 否 ──→ 它可能只是个配置工具或壳
```

把 Squirrel 套进这个流程：

1. Squirrel 的 Swift 源码里**没有**拼音词库、字频统计这些核心逻辑。
2. 它收到键盘事件后，会调用 librime 提供的 `process_key` 等函数，把按键「转手」交给 librime。
3. librime 算出候选词后，Squirrel 再把结果画到候选面板上、提交给当前 App。

所以 Squirrel 是**前端**，librime 才是**引擎**。SKILL.md 里有一句非常精确的概括，我们等下在源码精读里引用。

#### 4.1.3 源码精读

README 第一段就点明了「被引擎驱动」这件事：

> 今由　中州韻輸入法引擎／Rime Input Method Engine　及其他開源技術強力驅動

[README.md:8-9](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L8-L9) —— 这两行说明 Squirrel 是「由 Rime 引擎驱动」的，已经暗示了前端与引擎的分工。

更明确的定位写在 SKILL.md 的开篇：

> Use this skill when making changes to Squirrel, a macOS InputMethodKit frontend for librime.

[SKILL.md:8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/SKILL.md#L8) —— 一句话定义：**Squirrel 是一个面向 librime 的 macOS InputMethodKit 前端**。这是整个项目最重要的一句定位。

至于「程序入口在哪里」，SKILL.md 的仓库地图也直接给出：

> `Squirrel/Sources/Main.swift`: process entry point, command-line maintenance commands, IMK server creation, app setup, and global librime startup.

[SKILL.md:14](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/SKILL.md#L14) —— `Main.swift` 是整个程序的入口，负责创建 IMK 服务并启动全局 librime。这一行的细节会在后续「程序入口与启动流程」（u1-l4）那一讲展开，本讲你只需记住「入口在 Main.swift」。

#### 4.1.4 代码实践

**实践目标**：亲手从项目文档里找出「Squirrel 是前端」的证据，而不是听我转述。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，定位到第 8–9 行，找到「由……引擎……驅動」字样。
2. 打开 `SKILL.md`，定位到第 8 行，找到 "a macOS InputMethodKit frontend for librime" 这句话。
3. 在 SKILL.md 里搜索 `frontend` 一词，数一数它出现了几次（这能侧面说明「前端」这个定位在项目里有多核心）。

**需要观察的现象**：

- README 用「驱动」一词表达依赖关系；SKILL.md 用 "frontend" 一词直接给出定位。两种说法指向同一个事实。
- `frontend` 在 SKILL.md 里反复出现，说明它是贯穿整个架构的核心概念。

**预期结果**：你能用一句话向别人解释——「Squirrel 是 librime 引擎在 macOS 上的前端外壳」。

#### 4.1.5 小练习与答案

**练习 1**：如果 librime 这个引擎不存在，Squirrel 还能不能打出汉字？为什么？

> **参考答案**：不能（至少不能用现在的代码打出）。Squirrel 自己不包含拼音转换逻辑，它完全依赖 librime 来计算候选词；引擎没了，前端就只是一个收不到结果的空壳。

**练习 2**：「鼠鬚管」这个中文名和英文代号「Squirrel」之间，共同点是什么？

> **参考答案**：都带「鼠」字——中文名「鼠鬚管」来自鼠须毛笔，英文名「Squirrel（松鼠）」是对「鼠」的转译；前者强调书写工具，后者强调动物意象。

### 4.2 Rime 引擎生态（librime / plum / Sparkle）

#### 4.2.1 概念说明

Squirrel 之所以能工作，是因为它站在三个外部组件的肩膀上：

| 组件 | 角色 | 一句话作用 |
| --- | --- | --- |
| **librime** | 引擎（核心） | 真正做「按键 → 候选词」转换的 C++ 库，Squirrel 通过动态库（dylib）调用它。 |
| **plum**（東風破） | 配置管理器 | 用来获取/管理**输入方案**（schema）和词库，决定「装哪些输入法规则」。 |
| **Sparkle** | 自动更新框架 | macOS 上广泛使用的第三方库，负责检查并安装 Squirrel.app 的新版本。 |

此外还有一个常被一起提到的组件 **OpenCC（開放中文轉換）**，它负责简繁转换等功能，是 librime 运行时依赖的数据/库。

你可能会问：既然 librime 是引擎，为什么 Squirrel 仓库里还有 plum 和 Sparkle？因为 Squirrel 作为一个**完整的发行版**，不仅要能打字（librime），还要自带默认方案（plum 提供数据），还要能自我升级（Sparkle）。三者共同构成了「开箱即用的 macOS 输入法」。

#### 4.2.2 核心流程：依赖与数据如何在系统里流动

可以从两个时间维度理解这套生态：

**构建时（编译打包阶段）**：

```text
git submodule: librime   ──编译──→ librime.1.dylib  （引擎动态库）
git submodule: plum      ──取数据──→ 默认 schema / 词库（方案数据）
git submodule: Sparkle   ──集成──→ Sparkle.framework （更新框架）
            ↓ 一并打包进 Squirrel.app
```

这三个组件在仓库里都以 **git submodule** 的形式存在，构建时被拉取、编译或拷贝进最终的 `Squirrel.app`。

**运行时（用户打字阶段）**——一条按键的完整旅程（摘自 SKILL.md 的「text path」）：

```text
NSEvent（macOS 键盘事件）
  → SquirrelInputController.handle      （前端：收事件）
  → processKey
  → rimeAPI.process_key                 （前端→引擎：把按键交给 librime）
  → rimeUpdate
  → get_commit / get_status / get_context（前端：取回引擎结果）
  → client.insertText / setMarkedText   （前端→当前 App：上屏）
     + SquirrelPanel.update             （前端：刷新候选面板）
```

这条链路把「前端 / 引擎」的分工体现得淋漓尽致：**前半段（收事件、画 UI）是 Squirrel 的活，中间（process_key、算候选）是 librime 的活。** 记住这条链路，它就是第二单元「输入处理主链路」整条学习路线的骨架。

#### 4.2.3 源码精读

先看三个组件如何以子模块身份进入仓库：

```ini
[submodule "librime"]
  path = librime
  url = https://github.com/rime/librime.git
[submodule "plum"]
  path = plum
  url = https://github.com/rime/plum.git
[submodule "Sparkle"]
  path = Sparkle
  url = https://github.com/sparkle-project/Sparkle
```

[.gitmodules](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.gitmodules) —— 这里清清楚楚地登记了三个子模块：librime 和 plum 来自 Rime 官方组织（`rime/`），Sparkle 来自独立的 `sparkle-project` 组织。

再看 README 如何向用户介绍 plum 的用途：

> 使用 /plum/ 配置管理器獲取更多輸入方案。

[README.md:54](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L54) —— plum 的定位是「配置管理器」，用来获取更多输入方案。

最后是 README 引用的开源软件清单（节选）：

```text
* librime  (New BSD License)
* OpenCC / 開放中文轉換  (Apache License 2.0)
* plum / 東風破 (GNU Lesser General Public License 3.0)
* Sparkle  (MIT License)
```

[README.md:82-95](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L82-L95) —— 这段「致谢」清单列出了 Squirrel 引用的全部开源软件，其中就包含本模块讲的三件套（外加 OpenCC）。注意它们的授权各不相同，这点在下一模块会用到。

至于那条「按键旅程」的链路，原文在 SKILL.md：

[SKILL.md:138-140](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/SKILL.md#L138-L140) —— 这三行用箭头串起了从 `NSEvent` 到上屏的完整数据流，是理解前端/引擎分工的最佳一张图。

#### 4.2.4 代码实践

**实践目标**：把「三个组件 + 一条按键链路」在自己脑子里走一遍，并用工具核对。

**操作步骤**：

1. 在仓库根目录查看 `.gitmodules`，确认 `librime`、`plum`、`Sparkle` 三个子模块的 `path` 和 `url`。
2. 打开 README 第 82–95 行的清单，圈出 librime、plum、Sparkle、OpenCC 四项及其授权。
3. 打开 SKILL.md 第 138–140 行，把那条 `NSEvent → ... → SquirrelPanel.update` 的链路抄一遍，并在「交给引擎」的那一步（`rimeAPI.process_key`）旁边画条竖线，把链路切成「前端」和「引擎」两半。

**需要观察的现象**：

- 三个子模块的 url 分别属于 `rime/`（librime、plum）和 `sparkle-project/`（Sparkle）。
- 按键链路里，`process_key` 之前的事都发生在 Squirrel（Swift），`process_key` 本身是调用 librime（C/C++）。

**预期结果**：你能指着链路说「这一步是前端干的，这一步是引擎干的」。

#### 4.2.5 小练习与答案

**练习 1**：用户想给 Squirrel 增加「双拼」输入方案，应该主要靠哪个组件？为什么？

> **参考答案**：主要靠 **plum**。plum 是配置/方案管理器，专门用来获取和管理输入方案；librime 只负责「按方案规则做转换」，方案本身（规则文件、词库）要由 plum 提供。

**练习 2**：librime、plum、Sparkle 三者，哪一个**不**属于 Rime 官方项目？依据是什么？

> **参考答案**：**Sparkle** 不属于 Rime 官方。依据是 `.gitmodules` 里它的 url 指向 `sparkle-project/Sparkle`，而 librime、plum 都指向 `rime/` 组织。

**练习 3**：在按键链路 `NSEvent → ... → SquirrelPanel.update` 里，`rimeAPI.process_key` 这一步为什么是「前端 → 引擎」的分界点？

> **参考答案**：因为 `process_key` 是 librime（引擎）暴露给 Squirrel（前端）调用的接口；调用它之前，事件还在前端手里；调用它之后，转换逻辑交给引擎，前端只能等待结果回来。所以它是天然的分界点。

### 4.3 授权、平台与跨平台发行版关系

#### 4.3.1 概念说明

三个必须知道的事实：

1. **授权**：Squirrel 采用 **GPL v3**（GNU General Public License 第 3 版）。GPL 是一种「强 copyleft」的开源协议——你可以自由使用、修改、分发，但你发布基于它修改后的作品时，也必须以 GPL 开源、并公开源码。
2. **平台**：Squirrel **只支持 macOS**，且要求 **macOS 13.0 或更高版本**。
3. **跨平台**：Rime 是一个跨平台输入法家族。Squirrel 只是 macOS 这一员，它在其他操作系统上有「兄弟姐妹」：
   - **中州韻**（ibus-rime、fcitx-rime）—— Linux 发行版
   - **小狼毫**（Weasel）—— Windows 发行版

   它们共享同一个引擎 librime，只是各自换了对应平台的「前端」。

#### 4.3.2 核心流程：Rime 如何做到「一次引擎，多平台前端」

Rime 的跨平台策略可以画成这样：

```text
                    ┌─────────────────┐
                    │   librime 引擎   │   ← 平台无关的核心（C++）
                    │  （拼音/候选逻辑）│
                    └────────┬────────┘
                             │ 各平台通过各自的前端调用
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   macOS 前端            Linux 前端           Windows 前端
   Squirrel 鼠鬚管     中州韻 ibus/fcitx      小狼毫 Weasel
   （IMK + Swift）     （ibus/fcitx + C++）   （TSF + C++）
```

这是一种典型的「**核心与外壳分离**」架构：把不随平台变化的部分（引擎）做成共享库，把随平台变化的部分（事件接收、UI 绘制、系统注册）做成各平台专属前端。好处是——改拼音算法只需改一处（librime），所有平台同时受益；而每个平台又能用最原生的方式跟系统对接。

这就是为什么本手册会反复出现「这件事归 AppDelegate / InputController（前端）」「那件事归 librime（引擎）」的分工——它正是这套架构在源码层面的投影。

#### 4.3.3 源码精读

授权条款写在 README 显眼位置：

> 授權條款：[GPL v3](https://www.gnu.org/licenses/gpl-3.0.en.html)

[README.md:19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L19) —— 明确声明 GPL v3 授权。仓库根目录的 `LICENSE.txt` 是对应的完整协议文本。

平台要求在「安裝輸入法」一节：

> 本品適用於 macOS 13.0+

[README.md:31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L31) —— 仅支持 macOS 13.0 及以上。

跨平台发行版关系紧跟其后：

> 您可能還需要 Rime 用於其他操作系統的發行版：
>   * 【中州韻】（ibus-rime、fcitx-rime）用於 Linux
>   * 【小狼毫】用於 Windows

[README.md:23-26](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L23-L26) —— Squirrel（鼠鬚管）对应 macOS，中州韻对应 Linux，小狼毫对应 Windows，三者同属 Rime 家族。

#### 4.3.4 代码实践

**实践目标**：从文档里独立核实授权、平台、跨平台三项事实。

**操作步骤**：

1. 打开 `README.md` 第 19 行，确认授权是 GPL v3；再打开根目录 `LICENSE.txt`，确认它确实是 GPL v3 的完整文本（文件开头通常有 "GNU GENERAL PUBLIC LICENSE Version 3" 字样）。
2. 打开 `README.md` 第 31 行，确认最低系统版本是 macOS 13.0+。
3. 打开 `README.md` 第 23–26 行，把三个平台的发行版名称填进下表：

   | 操作系统 | Rime 发行版名称 |
   | --- | --- |
   | macOS | ______（鼠鬚管） |
   | Linux | ______ |
   | Windows | ______ |

**需要观察的现象**：三项事实都能在 README 的前 31 行内全部核实，无需读代码。

**预期结果**：填好的表格是：macOS → Squirrel 鼠鬚管；Linux → 中州韻（ibus-rime / fcitx-rime）；Windows → 小狼毫。

> 待本地验证：`LICENSE.txt` 的确切开头措辞建议你亲手 `cat` 一下确认，本讲不展开其全文。

#### 4.3.5 小练习与答案

**练习 1**：一家公司想把 Squirrel 的代码改一改、闭源打包成自己的商业输入法出售。根据 GPL v3，这允许吗？

> **参考答案**：不允许。GPL v3 要求：发布基于 GPL 代码的衍生作品时，必须同样以 GPL 开源并公开源码。「闭源商业发行」违反这一要求。

**练习 2**：为什么 Rime 的 Linux、macOS、Windows 三个发行版能共用同一套拼音算法，却各自有不同的 UI？

> **参考答案**：因为它们共享同一个平台无关的引擎 librime（算法在这里），而每个发行版只是为各自平台编写的前端（负责事件与 UI）。算法升级改 librime 一处即可，UI 则按平台原生方式各写各的。

**练习 3**：Squirrel 要求 macOS 13.0+。如果你在一台 macOS 12 的机器上安装，预期会发生什么？

> **参考答案**：很可能无法正常安装或运行（系统提示版本不兼容，或安装后无法被系统加载为输入法）。具体表现「待本地验证」，但官方明确声明只支持 13.0+。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个贯穿性小任务（即本讲规格里要求的实践任务）：

**任务**：阅读 `README.md` 与 `SKILL.md`，完成以下三件事。

1. **列出 Squirrel 运行所依赖的三个外部组件**，各用一句话说明作用。参考答案模板：

   | 组件 | 一句话作用 |
   | --- | --- |
   | librime | ______ |
   | plum | ______ |
   | Sparkle | ______ |

2. **指出 Squirrel 自己是「引擎」还是「前端」**，并给出**两条**来自项目文档的证据（一条来自 README，一条来自 SKILL.md），标明文件名和行号。

3. **画一张简易架构图**，包含：macOS 键盘事件 → Squirrel（前端）→ librime（引擎）→ 候选结果 → Squirrel 面板 → 当前 App 上屏。在图上用颜色或记号标出「前端负责的段落」和「引擎负责的段落」。

**操作提示**：

- 组件作用可直接参考本讲 4.2.1 的表格，但请用你自己的话重写一遍，不要照抄。
- 两条证据分别对应 [README.md:8-9](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L8-L9)（「由引擎驱动」）与 [SKILL.md:8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/SKILL.md#L8)（"frontend for librime"）。
- 架构图可参考本讲 4.2.2 的按键链路。

**预期结果**：你应当得到一张能挂在墙上向同事解释「Squirrel 是什么」的图，外加一段不超过 100 字的定位说明。

## 6. 本讲小结

- **Squirrel（鼠鬚管）是 Rime 输入法在 macOS 上的前端发行版**，它本身不做中文转换，真正的转换交给引擎 librime。
- 它依赖三个关键外部组件：**librime（引擎）、plum（方案/配置管理器）、Sparkle（自动更新）**，三者都以 git submodule 形式存在于仓库中。
- 授权为 **GPL v3**（强 copyleft，衍生作品须同样开源），运行平台为 **macOS 13.0+**。
- Rime 通过「**共享 librime 引擎 + 各平台专属前端**」实现跨平台：macOS 用 Squirrel、Linux 用中州韻、Windows 用小狼毫。
- 一条按键的旅程是：`NSEvent → Squirrel 收事件 → librime.process_key（前端→引擎）→ 取回候选 → Squirrel 画面板/上屏`。这条链路是后续所有讲义的主干。
- 阅读源码时，请始终在脑中区分「这件事是 Squirrel（Swift 前端）做的」还是「librime（C/C++ 引擎）做的」。

## 7. 下一步学习建议

本讲建立了「前端 vs 引擎」的认知框架，接下来建议按以下顺序继续：

1. **u1-l2 仓库目录结构**：动手看 `sources/`、`resources/`、`data/`、`package/` 这些目录到底装了什么，把本讲提到的子模块（librime/plum/Sparkle）在磁盘上对应起来。
2. **u1-l5 macOS 输入法（IMK）基础概念**：本讲只点了「Squirrel 是 IMK 前端」，但没展开 IMK 是什么。如果你想真正读懂 `SquirrelInputController`，IMK 的 `IMKServer` / `IMKInputController` / marked text 是必须先补的基础。
3. 暂时不必深入 librime 源码——先把 Squirrel 这一侧（前端）的启动、配置、UI 读完，第二单元再沿按键链路一步步走到 `rimeAPI.process_key` 那道「前端→引擎」的边界。

> 提示：本手册后续每一讲都会标注「这段代码属于前端还是引擎」。如果你在某讲里感到混乱，回到本讲复习这条「壳与芯」的分界线即可。
