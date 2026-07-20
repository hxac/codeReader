# 项目定位与核心概念

## 1. 本讲目标

本讲是整套 plum 学习手册的第一篇。读完本讲，你应该能够：

- 说清楚 **plum 是什么**、它在 Rime 输入法生态里扮演什么角色、为用户解决了什么问题。
- 准确区分四个最容易混淆的核心概念：**输入方案（schema）**、**词典（dictionary）**、**数据包（package）**、**配方（recipe ℞）**。
- 认识 plum 官方维护的包集合，并能说出它是如何分类的。

本讲**只读 `README.md`**，不涉及任何脚本逻辑。目的是先建立"大局观"，从第二篇开始我们才会真正进入代码。

## 2. 前置知识

阅读本讲前，你只需要了解以下通俗概念：

- **输入法（input method）**：让你用键盘打字的软件。比如系统自带的拼音、五笔。
- **Rime（中州韻輸入法引擎）**：一个开源的输入法"引擎"。它本身不是某个具体的拼音或五笔，而是一个**解释器**——你给它一套"规则"，它就能按这套规则把你的按键转换成汉字。
- **GitHub**：一个托管代码的网站。plum 以及它管理的所有方案，都以仓库（repository）的形式放在 GitHub 上的 `rime` 组织下。

你可以这样理解 Rime 的设计哲学：**引擎和规则是分离的**。Rime 引擎本身不知道怎么打拼音，所有的拼音、五笔、粤拼规则，都以"配置文件"的形式存在用户目录里。plum 要做的事，就是把合适的配置文件**搬运**到你的用户目录。

> 术语提示：本讲会出现"用户目录（Rime user directory）"这个词，指的是 Rime 引擎启动时读取配置的文件夹（例如 Linux 上常是 `~/.config/ibus/rime`）。它的具体路径因操作系统和前端而异，本讲先不展开，后面专门有一讲讲"前端识别"。

## 3. 本讲源码地图

本讲只涉及一个文件，但它信息量很大，是理解整个项目的入口：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的"说明书"。定义了项目定位、核心概念，并给出了一份官方包索引和基本用法。本讲所有结论都来自这里。 |

后面的讲义会陆续引入 `rime-install` 入口脚本和 `scripts/` 目录下的各个模块，本讲暂不需要。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 项目定位与解决的问题** —— plum 到底是干什么的。
2. **4.2 四个核心概念** —— schema / dictionary / package / recipe。
3. **4.3 官方包索引分类** —— plum 维护了哪些包、如何归类。

### 4.1 项目定位与解决的问题

#### 4.1.1 概念说明

plum（中文名"東風破"）是一个**配置管理器（configuration manager）**。它不是输入法引擎本身，而是为 Rime 引擎服务的一个"搬运工 + 管家"。

一句话定位：**plum 帮 Rime 用户从 GitHub 上下载并更新输入方案的配置文件，并安装到 Rime 用户目录。**

它要解决的痛点很具体：

- Rime 的方案以大量 YAML 配置文件和词典文件的形式散落在各个 GitHub 仓库里。
- 用户手动 `git clone`、手动拷贝文件、手动处理更新，既繁琐又容易出错。
- 不同操作系统、不同前端（ibus / Squirrel / Weasel / fcitx）的用户目录还不一样。

plum 把这些步骤封装成一条命令，让用户"一行命令装好一套默认配置"。

#### 4.1.2 核心流程

从用户视角，plum 工作的高层流程是这样的（本讲只讲"做什么"，"怎么做"在后面讲义展开）：

```text
用户运行 rime-install 命令
        │
        ▼
plum 根据"包名 / 配方"定位到 GitHub 上的仓库
        │
        ▼
从 GitHub 下载（clone 或下载 ZIP）对应的配置文件
        │
        ▼
把源文件安装到 Rime 用户目录（rime_dir）
        │
        ▼
Rime 引擎下次启动时读取这些配置，新方案即可使用
```

注意第三步：README 明确说明，plum 只负责"下载并安装源文件"，**它不会替你启用新方案**——也就是不会自动把新方案加入 Rime 的 `default.yaml` 的 `schema_list`。启用方案仍需用户在 Rime 的设置界面操作。这是一个重要的事实边界。

#### 4.1.3 源码精读

README 开篇第一句就给出了项目定位——它同时是一个"配置管理器"和"输入方案仓库"：

[README.md:3-5](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L3-L5) 这两行是项目标题"東風破 /plum/"和副标题"Rime configuration manager and input schema repository"，点明了 plum 的双重身份。

Introduction 段落把定位说得更清楚：plum 是 Rime 引擎的配置管理工具，目的是帮用户安装和更新默认配置与一组由 Rime Developers 维护的数据包：

[README.md:13-18](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L13-L18) 说明 plum 是 Rime 输入法引擎的配置管理器，专为用户安装/更新默认配置与官方数据包而设计。

plum 的能力不止于官方包，它同样兼容个人配置和第三方方案：

[README.md:20-21](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L20-L21) 指出 plum 也支持托管在 GitHub 上的个人配置，以及第三方开发者发布的输入方案包。

关于"安装但不启用"这条边界，README 在用法说明里写得很明确：

[README.md:104-105](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L104-L105) 说明一行命令会运行 `rime-install` 下载预设包并把源文件安装到 Rime 用户目录，括号里特别注明"它不会替你启用新方案（Yet it doesn't enable new schemas for you）"。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手从 README 里提炼 plum 的价值。

1. **实践目标**：用自己的话讲清楚"没有 plum 会怎样，有了 plum 又会怎样"。
2. **操作步骤**：
   - 打开 `README.md`，重点读 `Introduction`（第 11–35 行）和 `Usage`（第 91–106 行）两段。
   - 想象一个新用户：他刚装好 Rime，想用拼音，但用户目录是空的。在没有 plum 的情况下，他要做什么？有了 plum 之后呢？
3. **需要观察的现象**：留意 README 中提到的几个关键词——"install and update"、"preset packages"、"Rime user directory"。
4. **预期结果**：你应该能写出类似这样一段话——"plum 把'找方案仓库 → 下载 → 放到正确目录 → 后续还能更新'这套重复劳动压缩成了一条命令，并且能适配不同前端的不同用户目录。"
5. 本实践为阅读理解，不需要运行命令，**无需本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：plum 是 Rime 引擎本身吗？请引用 README 原文说明。

> **答案**：不是。README 第 13 行明确说 plum 是"a configuration manager for Rime input method engine"——它是 Rime 引擎的**配置管理器**，而非引擎本身。

**练习 2**：装好一个方案之后，它会立刻在 Rime 里生效吗？

> **答案**：不会。README 第 104–105 行括号里说明，plum 只负责下载并安装源文件，**不会替你启用新方案**（doesn't enable new schemas），启用需要在 Rime 设置里手动操作。

---

### 4.2 schema / dictionary / package / recipe 四个核心概念

#### 4.2.1 概念说明

这是本讲最重要、也最容易混淆的部分。Rime 把"怎么打字"这件事拆成了几个层次，plum 的文档里反复出现这几个词。我们一个一个讲。

| 概念 | 中文名 | 一句话解释 | 典型文件名 |
| --- | --- | --- | --- |
| **schema**（输入方案） | 輸入方案 | 定义一种具体输入法的全部规则：按键如何被解释、有哪些功能开关、调用哪个词典等 | `<schema_id>.schema.yaml` |
| **dictionary**（词典） | 韻書 | 一个词库，提供"编码 → 候选词"的映射数据，被方案引用 | `*.dict.yaml` |
| **package**（数据包） | 数据包 | 一个可分发的整体，包含一个或多个互相关联的方案及其词典，也可以是普通配置/数据文件 | 一个 GitHub 仓库 |
| **recipe**（配方） | 配方，符号 ℞ | 一段"可复用的配置动作"。数据包本身就可以是一种配方（最常见情况） | 用 ℞ 标记 |

用做菜来打个比方：

- **dictionary** 像"食材清单"（有哪些词）。
- **schema** 像"菜谱"（怎么把食材做成菜——也就是怎么把按键变成汉字）。
- **package** 像"一整套食材 + 菜谱的打包快递盒"，开箱就能做一道菜。
- **recipe** 像"加工指令"——比如"从这套食材里只取一部分，或加点调料"。快递盒本身（package）可以当作一条最简单的 recipe。

#### 4.2.2 核心流程

这四个概念在 plum 的实际使用中是这样配合的：

```text
用户指定一个 package（例如 luna-pinyin）
        │
        ▼
package 内部包含：1 个或多个 schema（如 luna_pinyin.schema.yaml）
        │
        ▼
每个 schema 通常引用 1 个 dictionary（如 luna_pinyin.dict.yaml）
        │
        ▼
plum 把整个 package 当作一条 recipe（℞）来执行
        │
        ▼
执行结果：相关文件被安装到 Rime 用户目录
```

关键关系：**schema 引用 dictionary，package 打包 schema 与 dictionary，recipe 描述"如何安装/加工一个 package"。**

> 一个细节：README 提到，未来 recipe 会变得更细粒度——允许你"从包里只选一部分安装"，甚至"接受参数来定制"。目前最常见的做法是"整个数据包直接当成一条 recipe"。

#### 4.2.3 源码精读

README 的 Introduction 用三段话依次定义了 schema、package、recipe，这是理解整个项目概念模型的"权威出处"。

**schema 与 dictionary 的定义**：

[README.md:23-26](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L23-L26) 这里定义：输入方案（schema）定义一种具体输入法的规则，即用户按键序列如何被引擎解释；它由一个 `<schema_id>.schema.yaml` 配置文件组成，通常还有一个可选的词典文件 `*.dict.yaml`。

**package 的定义**：

[README.md:28-29](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L28-L29) 说明一个 package 可以包含一个或多个互相关联的方案及其附属词典；package 也适合发布 Rime 使用的通用配置文件和数据文件。

**recipe 的定义（注意 ℞ 符号）**：

[README.md:31-35](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L31-L35) 这几行是 recipe 的权威定义：在 plum 的术语里，一段可复用的配置被称为"配方（recipe）"，用符号 ℞ 表示；数据包本身就可以是一条 recipe（这是最常见情况）；未来 plum 还会支持更细粒度的配方，允许从包里选择要安装的内容，甚至接受参数来定制目标方案。

℞ 这个符号你在后面的包索引里会反复看到，它就是"这是一条配方"的标记。

#### 4.2.4 代码实践

这是一个**阅读 + 归类型实践**，帮助你把四个概念钉死。

1. **实践目标**：能拿到任意一个 Rime 文件，判断它属于哪个概念层次。
2. **操作步骤**：
   - 读 README 第 23–35 行的概念定义。
   - 对下面这些"线索"做分类（填 schema / dictionary / package / recipe）：
     - `luna_pinyin.dict.yaml`
     - `luna-pinyin`（一个 GitHub 仓库 `rime/rime-luna-pinyin`）
     - `luna_pinyin.schema.yaml`
     - README 包索引里 `℞ luna-pinyin` 前面那个 ℞
3. **需要观察的现象**：注意文件名后缀——`.schema.yaml` 总是方案，`.dict.yaml` 总是词典；而仓库级别的名字（如 `luna-pinyin`）是 package。
4. **预期结果**：`luna_pinyin.dict.yaml`→dictionary；`luna-pinyin`（仓库）→package；`luna_pinyin.schema.yaml`→schema；`℞` 符号→recipe。
5. 本实践为阅读归类，**无需本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个 package 里可不可以有多个 schema？

> **答案**：可以。README 第 28 行说"A package may contain one or several interrelated input schemata"，即一个包可以包含一个或多个互相关联的方案。

**练习 2**：dictionary 文件的命名规律是什么？它和 schema 文件如何区分？

> **答案**：dictionary 文件命名为 `*.dict.yaml`，schema 文件命名为 `<schema_id>.schema.yaml`（README 第 25–26 行）。靠后缀 `.dict.yaml` 与 `.schema.yaml` 区分。

**练习 3**：为什么说"数据包本身就可以是一条 recipe"？

> **答案**：因为最简单的安装动作就是"把这个包里的文件原样装到用户目录"，这本身就是一条可复用的配置指令（recipe）。更复杂的 recipe 才会在此基础上做"挑选文件 / 修改内容"等加工（README 第 33–35 行）。

---

### 4.3 官方包索引分类

#### 4.3.1 概念说明

plum 的 README 维护着一份**官方包索引（Packages）**，列出了由 Rime Developers 在 GitHub 上分别维护的各个数据包。这些包构成了 plum 的"默认货架"——用户用一行命令就能装其中的预设组合。

README 把这些包分成 **4 个大类**：

1. **Essentials（基础）**：所有用户都需要的最基础配置。
2. **Phonetic-based input methods（基于读音的输入法）**：拼音、注音、粤拼等"按发音输入"的方案，按语言/方言再细分。
3. **Shape-based input methods（基于字形的输入法）**：五笔、仓颡等"按字形输入"的方案。
4. **Miscellaneous（杂项）**：emoji、国际音标等辅助包。

> 概念提示："Phonetic"指"语音的、基于读音的"，"Shape"指"字形"。这是中文输入法最大的两种设计思路：拼音类按发音编码，五笔/仓颡类按字形拆分编码。

#### 4.3.2 核心流程

这 4 个分类是按"用户需求"组织的，逻辑如下：

```text
官方包索引（共 22 个包）
├── Essentials（2 个）       —— 任何方案都要用到的基础
│     ├── prelude（基础配置）
│     └── essay（八股文，共享词库与语言模型）
├── Phonetic-based（12 个）  —— 按读音输入
│     ├── 现代标准官话：luna-pinyin, terra-pinyin, bopomofo, pinyin-simp
│     ├── 拼音衍生：    double-pinyin, combo-pinyin, stenotype
│     ├── 其他现代方言：cantonese, jyutping, wugniu, soutzoe
│     └── 中古汉语：    middle-chinese
├── Shape-based（6 个）      —— 按字形输入
│     └── stroke, cangjie, quick, wubi, array, scj
└── Miscellaneous（2 个）    —— 辅助工具
      └── emoji, ipa
```

另外，README 在"Advanced usage"里还提到一种**按"安装范围"**的横向划分：`:preset`（预设）、`:extra`（额外）、`:all`（全部）三档集合。这和上面的"按内容分类"是两个不同维度——一个是"这是什么输入法"，一个是"默认装多少"。三档集合的具体清单在 `*-packages.conf` 文件里，那是后续讲义（u2-l7）的内容，本讲只需知道有这三档即可。

> 一个可以自己核对的事实：`:all` 恰好等于 `:preset` 与 `:extra` 的合集，加起来共 22 个包，与上面按内容分类的总数一致。

#### 4.3.3 源码精读

包索引的总体说明在 Packages 段开头：

[README.md:37-42](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L37-L42) 说明这是一份由 Rime Developers 分别维护的包索引，旨在为多数用户提供合理的默认配置，覆盖多种中文输入法，包括基于现代方言和历史汉语音韵的方案。

**Essentials 类**（2 个基础包）：

[README.md:46-49](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L46-L49) 列出基础类两个包：`prelude`（基础配置，提供 Rime 的默认设置）和 `essay`（八股文，共享词库与语言模型）。

**Phonetic-based 类**——这一类内容最多，README 又按语言/演变细分成 4 小组：

[README.md:51-75](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L51-L75) 这是整个"基于读音"的大类，依次包含：现代标准官话（luna-pinyin 朙月拼音、terra-pinyin 地球拼音、bopomofo 注音、pinyin-simp 袖珍简化字拼音）、拼音衍生（double-pinyin 双拼、combo-pinyin 宮保拼音、stenotype 打字速记法）、其他现代方言（cantonese 粤拼、jyutping 粤拼无声调、wugniu 上海吴语、soutzoe 苏州吴语）、中古汉语（middle-chinese 中古汉语拼音）。

**Shape-based 类**：

[README.md:77-84](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L77-L84) 列出基于字形的输入法：stroke 五笔画、cangjie 仓颉、quick 速成（简化的仓颉）、wubi 五笔字型、array 行列输入法、scj 快速仓颉。

**Miscellaneous 类**：

[README.md:86-89](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L86-L89) 列出两个杂项包：`emoji`（用英文或拼音关键词输入 emoji）和 `ipa`（国际音标）。

#### 4.3.4 代码实践

这是本讲义**指定的主实践任务**，源自讲义规格。

1. **实践目标**：通读包索引，能复述 Essentials 与 Phonetic-based 两类的内容，并理解 plum 的价值。
2. **操作步骤**：
   - 打开 `README.md` 的 `## Packages` 段（第 37–89 行）。
   - 分别列出 **Essentials** 类与 **Phonetic-based** 类下的所有官方包。
   - 写一段自己的话，说明 plum 帮 Rime 用户解决了什么问题（可参考 4.1 的结论）。
3. **需要观察的现象**：
   - 注意每个包前面的 ℞ 符号——它提醒你"每个包都是一条 recipe"。
   - 注意 Phonetic-based 内部还分了 4 个语言/演变小组。
4. **预期结果**：
   - **Essentials（2 个）**：`prelude`、`essay`。
   - **Phonetic-based（12 个）**：
     - 现代标准官话：`luna-pinyin`、`terra-pinyin`、`bopomofo`、`pinyin-simp`
     - 拼音衍生：`double-pinyin`、`combo-pinyin`、`stenotype`
     - 其他现代方言：`cantonese`、`jyutping`、`wugniu`、`soutzoe`
     - 中古汉语：`middle-chinese`
   - 关于 plum 的价值，参考答案要点：把分散在多个 GitHub 仓库的方案/词典集中管理，提供一行命令安装与更新，并适配不同前端的用户目录；同时给出官方推荐的基础与进阶组合。
5. 本实践为阅读整理，**无需本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Essentials 类为什么只有 `prelude` 和 `essay` 两个？它们各自的作用是什么？

> **答案**：因为它们是几乎所有方案都要依赖的基础。`prelude` 提供 Rime 的默认设置（基础配置），`essay` 提供共享词库与语言模型（八股文）。其他具体输入法方案都建立在这套基础之上（README 第 46–49 行）。

**练习 2**：`luna-pinyin` 和 `stroke` 分别属于哪个大类？依据是什么？

> **答案**：`luna-pinyin` 属于 **Phonetic-based**（基于读音，它是朙月拼音），`stroke` 属于 **Shape-based**（基于字形，它是五笔画）。依据是 README 的分类标题，拼音按发音输入归入 Phonetic，五笔画按字形输入归入 Shape（README 第 51–89 行）。

**练习 3**：README 里还提到 `:preset` / `:extra` / `:all` 三档，它和 Essentials/Phonetic-based/Shape-based/Miscellaneous 这 4 个分类是什么关系？

> **答案**：它们是**两个不同维度**。4 个分类是"按输入法内容"组织的（这是什么输入法）；三档集合是"按安装范围"组织的（默认装多少），其中 `:all` 是 `:preset` 与 `:extra` 的合集。两者互相独立（README 第 109–115 行提到三档用法，包内容分类在第 46–89 行）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个"假装你在给朋友安利 plum"的小任务：

1. 用一句话告诉朋友 **plum 是什么**（用上"配置管理器"和"Rime 引擎"两个词）。
2. 朋友问"方案和词典有啥区别"，请用"菜谱 vs 食材清单"的比喻解释 **schema 与 dictionary 的关系**，并各举一个文件名例子。
3. 朋友想打繁体拼音，请你从官方包索引里给他推荐 **一个 Phonetic-based 类的包**，并说明它属于哪个语言/演变小组。
4. 提醒朋友一个**重要边界**：装完之后还要做什么？（提示：plum 不会替你做的那件事。）

**参考答案要点**：
1. plum 是 Rime 输入法引擎的配置管理器，帮你一行命令安装和更新输入方案。
2. schema 像菜谱（怎么把按键变成字，例：`luna_pinyin.schema.yaml`），dictionary 像食材清单（有哪些词，例：`luna_pinyin.dict.yaml`）；schema 引用 dictionary。
3. 推荐繁体拼音可用 `luna-pinyin`（朙月拼音，属"现代标准官话"小组）。
4. 还要自己在 Rime 设置里**启用**新方案，plum 只安装不启用。

## 6. 本讲小结

- plum（東風破）是 **Rime 引擎的配置管理器**，不是引擎本身；它从 GitHub 下载并更新方案，安装到 Rime 用户目录。
- 四个核心概念：**schema**（方案，`.schema.yaml`）、**dictionary**（词典，`.dict.yaml`）、**package**（数据包，一个仓库）、**recipe**（配方，符号 ℞）。
- 关系链：**schema 引用 dictionary，package 打包 schema + dictionary，recipe 描述如何安装/加工一个 package**；数据包本身就是最常见的 recipe。
- 重要边界：plum **只安装源文件，不自动启用新方案**。
- 官方包索引按内容分 **4 大类**：Essentials（2）、Phonetic-based（12）、Shape-based（6）、Miscellaneous（2），共 22 个包。
- 另有按安装范围的横向划分：`:preset` / `:extra` / `:all` 三档集合。

## 7. 下一步学习建议

本讲只读了 `README.md`，还完全没有接触代码。下一讲 **u1-l2《目录结构与脚本入口》** 将带你：

- 认识 plum 仓库的文件组成（入口脚本 `rime-install`、`scripts/` 模块目录、`*.conf` 包清单等）。
- 理解 `rime-install` 脚本如何**自举**（自动 clone plum 自身）并把请求转发给新版脚本。
- 为后续进入核心模块（模块加载、配方解析、安装主循环）打好基础。

建议在进入下一讲前，先在本机把整个仓库浏览一遍（`ls` 看根目录、`ls scripts/` 看模块），对照本讲的概念，试着猜猜每个文件大概对应哪个概念或哪项职责——这种"先猜后验证"的习惯会让你后面读源码时事半功倍。
