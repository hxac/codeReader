# Git 是什么：定位、历史与设计哲学

> 本讲是《Git 源码学习手册》的第 1 讲，属于入门层（beginner）。
> 我们不写一行配置、也不深入 C 源码细节，先从项目自身的话术里把「Git 到底是什么」讲清楚，为后续逐层拆解源码打好地基。

---

## 1. 本讲目标

读完本讲后，你应该能够：

1. 用一句话说清 Git 解决的问题，以及它和 CVS/SVN 这类传统版本控制系统的本质差别。
2. 理解 Git 最核心的设计思想——「内容寻址的对象数据库（content-addressable object database）」，并知道它为什么被 Linus Torvalds 戏称为「the stupid content tracker（愚蠢的内容追踪器）」。
3. 在仓库里找到 Git 官方为新手指引和贡献者准备的文档入口，知道出问题去哪里提问、去哪里读手册。
4. 从 `version.h` / `version.c` 看懂「版本号字符串」这个最小源码示例，建立「文档里的一句话对应到源码里某个符号」的直觉。

---

## 2. 前置知识

本讲面向零基础读者，不要求你写过 C，也不要求你精通命令行。你只需要带着下面这几个日常概念来读：

- **文件（file）**：磁盘上的一段有名字的内容，比如 `README.md`。
- **目录（directory / folder）**：装文件的容器，可以层层嵌套。
- **版本控制（version control）**：一种「把项目的每一次改动都记录下来、随时能回到过去某个状态」的工具。即使你没用过 Git，多半也用过 Word 的「修订历史」或网盘的「历史版本」，思路类似。
- **哈希（hash）**：把任意长度的内容喂给一个数学函数，得到一串固定长度、看起来像乱码的指纹。同样的内容一定得到同样的指纹；哪怕只改一个字符，指纹也会完全不同。Git 把这个指纹当作「内容的名字」。

> 术语提示：本讲反复出现的「仓库（repository）」就是「一个被 Git 管理的项目目录」，里面多了一个隐藏的 `.git` 子目录存放全部历史。这个 `.git` 在后续讲义里会被反复解剖。

---

## 3. 本讲源码地图

本讲主要读「项目自我介绍类」的文件，它们是理解 Git 定位的最佳入口：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `README.md` | 项目首页，一句话定位、许可证、社区入口 | 项目定位与许可、社区入口 |
| `version.h` | 声明版本字符串等对外符号的头文件 | 最小源码示例：版本号从哪来 |
| `version.c` | 定义 `git_version_string` 等符号 | 版本号在源码里的落地 |
| `Documentation/gittutorial.adoc` | 官方新手教程 | 「Git 跟踪的是内容而非文件」的设计思想 |
| `Documentation/MyFirstContribution.adoc` | 官方「首次贡献」指引 | 贡献者文档与社区入口 |

> 说明：本讲的「源码」既包括真正的 C 文件（`version.h` / `version.c`），也包括项目自带的文档。对入门讲而言，文档本身就是 Git 仓库里最重要、最权威的「源」之一。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 项目定位与许可**——Git 是什么、谁写的、用什么协议开源。
2. **4.2 内容寻址设计思想**——Git 区别于传统版本控制系统的灵魂。
3. **4.3 文档与社区入口**——出问题去哪里找答案、去哪里参与。

---

### 4.1 项目定位与许可

#### 4.1.1 概念说明

打开任何开源项目的第一步，都是先看它的 `README`。Git 仓库根目录的 `README.md` 第一行就给了它的「自我定位」：

> Git - fast, scalable, distributed revision control system
> （Git——快速、可扩展、**分布式**的版本控制系统）

这里有三个关键词：

- **fast（快速）**：几乎所有操作都在本地完成，不依赖网络。
- **scalable（可扩展）**：能扛住 Linux 内核这种百万级提交、数十万文件的巨型仓库。
- **distributed（分布式）**：每个开发者本地都拥有**完整的历史副本**，而不只是某个中央服务器的瘦客户端。

> 对比传统系统：CVS 和 SVN 属于**集中式（centralized）**版本控制——历史只存在一台中央服务器上，`checkout` 只取某个版本的工作副本，离线就几乎什么都做不了。Git 的「分布式」意味着 clone 之后，你的本地仓库和服务器在地位上是**对等**的。

`README.md` 还交代了项目的出身和许可证：

> Git is an Open Source project covered by the GNU General Public License version 2 … It was originally written by Linus Torvalds …

也就是说：Git 由 Linux 之父 Linus Torvalds 在 2005 年亲手写下的第一个版本起步，随后由全球开发者社区接力维护，整体采用 **GPL v2** 开源协议（部分子模块用与之兼容的其他协议）。

#### 4.1.2 核心流程：版本号字符串是怎么来的

为了让你第一次接触 C 源码时不至于发懵，我们挑一个最小的符号——**版本号字符串**，看看「文档里说的版本」是怎么落到代码里的。它经历的链路是：

1. **构建期生成宏**：`make` 时由 `GIT-VERSION-GEN` 脚本读取默认版本号，生成一个定义了 `GIT_VERSION` 宏的文件（通过 `version-def.h` 引入）。
2. **源码落地**：`version.c` 把 `GIT_VERSION` 宏的值赋给全局变量 `git_version_string`。
3. **头文件对外声明**：`version.h` 用 `extern` 声明这个变量，让别的源文件能用它。
4. **命令输出**：当用户敲 `git --version` 时，`help.c` 用 `git_version_string` 拼出 `git version <x.y.z>` 打印出来。

一句话概括：**版本号本质上是构建期注入的一个字符串常量，经过 `version.c` 落地、`version.h` 对外暴露，最终被命令行命令读取显示。**（细节会在第 2 讲「从源码构建」展开。）

#### 4.1.3 源码精读

先看 `README.md` 的定位与许可证原文（顺带也写明了「distributed」的定位）：

- [README.md:L3-L3](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L3-L3) — 标题行，一句话自我定位。
- [README.md:L6-L8](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L6-L8) — 正文首段，强调 fast / scalable / distributed，并提到「既提供高层操作，也开放对内部机制的完整访问」。
- [README.md:L10-L13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L10-L13) — 许可证（GPL v2）与作者（Linus Torvalds）声明。

再看最小的 C 示例。`version.h` 只用 `extern` 把符号「承诺」出去，本身不分配内存：

```c
/* version.h（节选） */
extern const char git_version_string[];
extern const char git_built_from_commit_string[];
```

> 这段 [version.h:L4-L8](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version.h#L4-L8) 只是声明：「别处有一个只读字符串 `git_version_string`，谁需要谁来用」。真正的赋值在下面的 `version.c` 里。

```c
/* version.c（节选） */
#include "version-def.h"            /* 由构建期生成，定义 GIT_VERSION 宏 */

const char git_version_string[] = GIT_VERSION;
const char git_built_from_commit_string[] = GIT_BUILT_FROM_COMMIT;
```

> 见 [version.c:L12-L13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version.c#L12-L13)。`GIT_VERSION` 这个宏的值在 `make` 时被注入；`git_built_from_commit_string` 则记录「这次构建是从哪个提交编译出来的」，便于排查二进制来源。

#### 4.1.4 代码实践

**实践目标**：验证「文档说的版本」与「源码里的版本符号」是同一条链路。

**操作步骤**：

1. 找一台已安装 Git 的机器（任意系统、任意 Git 版本均可，本讲不要求从源码编译——那是第 2 讲的事）。
2. 在终端运行：
   ```bash
   git --version
   ```
3. 打开本仓库的 [version.h:L4-L8](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version.h#L4-L8)，确认 `git_version_string` 只是一个 `extern` 声明。

**需要观察的现象**：终端打印出形如 `git version 2.x.x` 的一行。

**预期结果**：你看到的那串 `2.x.x` 数字，最终就来自 [version.c:L12](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version.c#L12) 里 `GIT_VERSION` 宏的值——这条链路在下一讲构建流程里会被完整还原。

> 若你尚未安装 Git 或无法运行命令，把 `git --version` 的输出标记为「待本地验证」即可，不影响理解。

#### 4.1.5 小练习与答案

**练习 1**：`version.h` 里为什么用 `extern const char git_version_string[];` 而不是直接定义 `const char git_version_string[] = "2.49.0";`？

> **参考答案**：把版本号「写死在头文件里」会让每一个 `#include "version.h"` 的源文件都各自拷贝一份定义，导致链接时出现重复符号。用 `extern` 只做声明、把唯一定义放在 `version.c`，既保证全程序只有一份字符串，又让版本号能由构建脚本集中注入。

**练习 2**：`README.md` 里说 Git 是「distributed」。请用一句话解释「分布式」对离线工作的意义。

> **参考答案**：因为 clone 后本地拥有完整历史，所以 `git log`、`git diff`、`git branch` 等绝大多数操作完全不需要联网，在飞机上也能正常查历史、切分支；只有 `push`/`pull`/`fetch` 等需要与远程同步时才联网。

---

### 4.2 内容寻址设计思想

#### 4.2.1 概念说明

如果说「分布式」是 Git 的躯干，那么「**内容寻址的对象数据库**」就是它的灵魂。官方新手教程 [gittutorial.adoc](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gittutorial.adoc) 在结尾把它点了出来：

> The object database is the rather elegant system used to store the history of your project--files, directories, and commits.
> （对象数据库是用来存储项目历史的相当优雅的系统——文件、目录、提交都在里面。）

这句话里有两个关键词要拆开看：

- **对象（object）**：在 Git 眼里，项目里的一切——某个文件的某次内容、某个目录的快照、某次提交记录、某个带说明的标签——统统都是「对象」。Git 不区分「文件」和「改了文件的哪几行」，它只关心「对象」。
- **内容寻址（content-addressable）**：平时我们用**名字**找东西（`简历.docx`）；Git 用**内容的哈希值**找东西。内容决定名字，而不是人决定名字。这就是 Linus 调侃的「the stupid content tracker」——它蠢在「只认内容」，但也正是这种简单让它极其可靠。

教程里还有一句直白的总结：

> Git tracks **content** not files.
> （Git 跟踪的是**内容**，而不是文件。）

对比一下传统系统：CVS/SVN 的核心模型是「**基于变更（changeset / delta）**」——记录「文件 A 第 3 行从 X 改成了 Y」；而 Git 的核心模型是「**基于快照（snapshot）**」——每次提交都把当时的所有内容存成一个完整快照（再用高效的方式压缩去重）。一个记「怎么改」，一个记「是什么」。

#### 4.2.2 核心流程：内容如何变成一个「有名字的对象」

把一段内容存进对象数据库，Git 做的事情可以概括为下面四步（伪代码）：

```
输入：任意一段字节 content（比如一个文件的内容）
# 1. 加头：在内容前拼上对象类型和长度
blob = "blob " + len(content) + "\0" + content
# 2. 算哈希：得到对象的名字（object id）
oid = SHA-1(blob)          # 新仓库也可选 SHA-256
# 3. 压缩：用 zlib 把 blob 压缩
packed = zlib_compress(blob)
# 4. 落盘：按名字分目录存放
write(".git/objects/" + oid[0:2] + "/" + oid[2:])
输出：oid（这段内容的「名字」）
```

关键性质由此而来：

- **同名即同内容**：两段完全一样的内容，哈希一定相同，Git 只存一份（天然去重）。
- **改一字符则全变**：内容哪怕只改一个标点，哈希就彻底变了，于是变成一个新对象（不可篡改、历史完整）。
- **名字是内容本身的指纹**：你只要报出哈希，Git 就能从 `.git/objects` 里取出对应内容——这就是「内容寻址」。

关于哈希长度，做个简单算术：SHA-1 是 160 位，每 4 位折算成 1 个十六进制字符，所以一个 SHA-1 对象名是

\[ \text{SHA-1 名长} = \frac{160}{4} = 40 \text{ 个十六进制字符} \]

而较新的 SHA-256（Git 已支持，称为 `sha256` 仓库）则是

\[ \text{SHA-256 名长} = \frac{256}{4} = 64 \text{ 个十六进制字符} \]

你在 `git log` 里看到的那些 `c82a22c39c…` 一长串，就是提交对象的名字。

> 边界提醒：以上是「概念流程」。真正读写对象的 C 实现（`write_object_file` 等）位于 `object-file.c`，会在第 6 单元「对象模型与对象存储」精讲，本讲不展开源码细节，以免越界。

#### 4.2.3 源码精读

本模块的「源」主要是官方教程对设计思想的直接表述：

- [Documentation/gittutorial.adoc:L16-L17](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gittutorial.adoc#L16-L17) — 教程开宗明义：讲解如何把项目导入 Git、做修改并与他人共享。
- [Documentation/gittutorial.adoc:L148-L156](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gittutorial.adoc#L148-L156) — 标题「Git tracks content not files」及解释：`git add` 对新旧文件一视同仁，都是「拍一张内容快照存进索引」。
- [Documentation/gittutorial.adoc:L628-L634](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gittutorial.adoc#L628-L634) — 教程结尾点出的两大基石：**对象数据库**（存文件/目录/提交的历史）与**索引文件**（目录树状态的缓存）。

而「愚蠢的内容追踪器」这个戏称，正来自项目首页：

- [README.md:L55-L66](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L55-L66) — Linus 解释名字 `git` 的由来，自嘲为「the stupid content tracker」，并列出几种含义（随机三字母、俚语、global information tracker 等）。

#### 4.2.4 代码实践

**实践目标**：亲手感受「内容决定名字」这件事——同样内容的对象名完全相同。

**操作步骤**：

1. 准备一段固定文字，比如 `hello git`。
2. 计算它的对象名（只算哈希、不写入仓库）：
   ```bash
   printf 'hello git\n' | git hash-object --stdin
   ```
3. 把同样内容**再算一次**，对比两次输出。
4. 换一个字（如 `hello Git`，大小写不同），再算一次，观察对象名是否变了。

**需要观察的现象**：第 2、3 步会输出**完全相同**的 40 个十六进制字符（SHA-1 仓库）；第 4 步只要内容不同，输出就**彻底改变**。

**预期结果**：你会直观看到「内容 → 哈希」是确定性映射，这正是对象数据库寻址的基础。具体的 40 字符串取值取决于你输入的字节，请以本机实际输出为准（**待本地验证**，本讲不预填可能误导的具体哈希值）。

> 想再进一步：加 `-w` 参数（`git hash-object -w --stdin`）会真正写入一个「松散对象」到 `.git/objects/` 下，文件名就是哈希值。这对应 4.2.2 流程的第 4 步。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 Git 的「按内容寻址」天然带去重？

> **参考答案**：因为对象名 = 内容哈希。两段完全相同的内容会得到相同的名字，Git 在写入前会发现该名字已存在，因此只保留一份。例如 100 个内容相同的文件，对象数据库里只会有 1 个 blob 对象。

**练习 2**：传统 CVS/SVN 记录「变更」，Git 记录「快照」。请各举一个生活化的比喻。

> **参考答案**：CVS/SVN 像「改错本」——只记录「第 3 页第 5 行把『张三』改成了『李四』」；Git 像「相册」——每次提交都对整本书拍一张完整照片，想看哪版直接翻到那一张，不依赖逐行反推。

**练习 3**：某 SHA-1 仓库里你看到对象名是 `ce8130…` 开头共 40 个十六进制字符。如果改用 SHA-256，对象名长度会变成多少？

> **参考答案**：64 个十六进制字符。因为 \(256/4=64\)。这也意味着同一仓库不会混用两种哈希，哈希算法在 `git init` 时就确定了。

---

### 4.3 文档与社区入口

#### 4.3.1 概念说明

Git 是一个体量庞大的成熟项目，它的「文档」本身就是一份宝藏，而且**全部都在仓库里**。这一模块的目标是让你记住「卡住时去哪儿」：

- **想学怎么用**：`Documentation/gittutorial.adoc`（新手教程）、`Documentation/giteveryday.adoc`（日常 20 条命令）、每条命令的 `Documentation/git-<命令>.adoc`。
- **想给项目贡献代码**：`Documentation/MyFirstContribution.adoc`（手把手教你提交第一个补丁）、`Documentation/SubmittingPatches`（补丁提交规范）、`Documentation/CodingGuidelines`（代码风格）。
- **想提问 / 报 bug / 看讨论**：邮件列表 `git@vger.kernel.org`，存档在 [lore.kernel.org/git](https://lore.kernel.org/git/)。
- **安全问题**：私下联系 `git-security@googlegroups.com`，不要公开贴 issue。

和很多现代项目不同，Git **以邮件列表为中心**协作——补丁通过邮件发送、在邮件里 review、用邮件合并。这一点和「提 GitHub PR」的体验很不一样，是初来者最需要建立的认知。

#### 4.3.2 核心流程：从「想用 / 想贡献」到「找到正确文档」

把上面的入口组织成一张「寻路图」：

```
你的诉求                    →  打开哪份文档 / 走哪条路
─────────────────────────────────────────────────────────
第一次用 Git                →  Documentation/gittutorial.adoc
想查某条命令                →  git help <命令>  或  Documentation/git-<命令>.adoc
想给 Git 贡献第一个补丁     →  Documentation/MyFirstContribution.adoc
想知道补丁格式 / 邮件礼仪   →  Documentation/SubmittingPatches
想看代码风格                →  Documentation/CodingGuidelines
遇到 bug / 提问             →  邮件列表 git@vger.kernel.org
遇到安全问题                →  私邮 git-security@googlegroups.com
```

记住这张表，你在后续读源码时就不会在几十个目录里迷路。

#### 4.3.3 源码精读

社区与文档入口的「源」同样来自项目自述：

- [README.md:L20-L26](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L20-L26) — 指路新手教程、日常命令集，并说明安装后可用 `man gittutorial` 或 `git help tutorial` 阅读文档。
- [README.md:L32-L36](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L32-L36) — 点明用户讨论与开发都在邮件列表 `git@vger.kernel.org` 上进行，欢迎在此提交 bug、功能请求和补丁。
- [README.md:L42-L45](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L42-L45) — 订阅方式与邮件列表存档地址。
- [Documentation/MyFirstContribution.adoc:L6-L9](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/MyFirstContribution.adoc#L6-L9) — 首次贡献教程的自我介绍：演示从改代码、送审到根据意见修改的端到端流程。
- [Documentation/MyFirstContribution.adoc:L31-L41](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/MyFirstContribution.adoc#L31-L41) — 说明 `git@vger.kernel.org` 是主邮件列表（代码评审、版本发布、设计讨论都在此），并强调「纯文本邮件、推荐内联回复」的礼仪。
- [Documentation/MyFirstContribution.adoc:L70-L77](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/MyFirstContribution.adoc#L70-L77) — 给出克隆仓库并进入目录的命令，是参与贡献的第一步。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍「文档寻路」，确认你能定位到上面这些入口。

**操作步骤**：

1. 在本仓库根目录，确认 `README.md`、`Documentation/gittutorial.adoc`、`Documentation/MyFirstContribution.adoc` 三份文件都存在。
2. 若已安装 Git，运行下面的命令，看 Git 自带的文档系统：
   ```bash
   git help tutorial      # 对应 gittutorial.adoc
   git help everyday      # 对应 giteveryday.adoc
   ```
3. 打开 [README.md:L32-L45](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L32-L45)，记下邮件列表地址和存档网址。

**需要观察的现象**：第 2 步会打开（或在终端分页显示）对应的手册页；如果系统未安装 man 工具或文档，命令会提示找不到——这不影响理解，标注为「待本地验证」即可。

**预期结果**：你建立了一份「卡住时先查哪份文档、再往哪个邮件列表提问」的清单。

#### 4.3.5 小练习与答案

**练习 1**：你想给 Git 修一个拼写错误，应该先读哪份文档？为什么 Git 不像很多项目那样「直接提 PR」？

> **参考答案**：先读 `Documentation/MyFirstContribution.adoc` 和 `Documentation/SubmittingPatches`。Git 以**邮件列表**为协作中心，贡献流程是把补丁通过邮件发给 `git@vger.kernel.org`、在邮件里评审，而不是依赖某个代码托管网站的 PR 按钮。

**练习 2**：发现了一个可能被远程利用的安全漏洞，正确的披露方式是？

> **参考答案**：私下联系 `git-security@googlegroups.com`，不要先在公开邮件列表或 issue 里贴出细节，以免在修复前被利用。

**练习 3**：`git help <命令>` 和 `Documentation/git-<命令>.adoc` 是什么关系？

> **参考答案**：它们是同一份文档的两种访问方式。`Documentation/git-<命令>.adoc` 是源文件；`git help <命令>`（或 `man git-<命令>`）是安装后由这套 `.adoc` 渲染出来的手册页。

---

## 5. 综合实践

> 这是贯穿本讲三模块的实践任务，也是本讲规格里指定的核心练习。

**任务**：阅读 [README.md](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md) 与 [Documentation/gittutorial.adoc](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gittutorial.adoc)，用自己的话写一段 **200 字以内** 的说明，回答：

> **Git 与传统版本控制系统（如 CVS/SVN）在设计理念上的两个关键差异是什么？**

**操作步骤**：

1. 精读 README 的定位段（[L6-L13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/README.md#L6-L13)）和教程的「Git tracks content not files」段（[L148-L156](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gittutorial.adoc#L148-L156)）。
2. 从本讲 4.1、4.2 里提炼出**两个**最具代表性的差异。
3. 把它们写成一段通顺的中文，控制在 200 字以内。

**需要观察的现象 / 评判标准**：你的答案应能体现出以下两点中的**至少两点**（任选两个即可）：

| 维度 | 传统（CVS/SVN） | Git |
| --- | --- | --- |
| 拓扑 | 集中式：历史只在中央服务器 | 分布式：本地拥有完整历史 |
| 存储模型 | 基于变更（记录「怎么改」） | 基于快照 + 内容寻址（记录「是什么」） |
| 寻址方式 | 按文件名 / 修订号 | 按内容哈希 |

**预期结果（参考范文，约 150 字）**：

> 两个关键差异：其一，Git 是**分布式**的——clone 后本地即拥有完整历史，绝大多数操作离线可用，而 CVS/SVN 是集中式，离线几乎无法工作；其二，Git 采用**内容寻址的对象数据库**，以内容哈希命名一切对象，记录的是项目快照而非逐行变更，因此天然去重、历史不可篡改，这与 CVS/SVN「按文件名、记录增量 delta」的模型截然不同。

> 写作提示：范文只是参考。请用自己的话重写，避免照抄；只要命中上表中两个维度、且表述准确即可。

---

## 6. 本讲小结

- **Git 是什么**：一个 fast / scalable / **distributed** 的版本控制系统，由 Linus Torvalds 在 2005 年开创，以 GPL v2 开源（见 `README.md`）。
- **设计灵魂**：Git 的核心是「**内容寻址的对象数据库**」——以内容哈希给一切对象命名，记录的是**快照**而非逐行变更，这正是它「跟踪内容而非文件」、天然去重、历史不可篡改的根源。
- **与传统系统的差别**：集中式 vs 分布式、变更模型 vs 快照模型、按名寻址 vs 按内容寻址。
- **最小源码示例**：`version.h` 用 `extern` 声明 `git_version_string`，`version.c` 把构建期注入的 `GIT_VERSION` 宏赋给它，构成「文档版本号 ↔ 源码符号」的一条完整链路。
- **文档与社区**：用 Git 看 `gittutorial.adoc`；想贡献看 `MyFirstContribution.adoc`；讨论在邮件列表 `git@vger.kernel.org`；安全问题私邮 `git-security@googlegroups.com`。
- **本讲定位**：只建立「Git 是什么、为什么这么设计、去哪找答案」的认知；真正的源码机制从下一讲「从源码构建」开始逐层展开。

---

## 7. 下一步学习建议

本讲是「入门层」的第一步，建议按下面的顺序继续：

1. **下一讲 u1-l2《从源码构建 git：Makefile 体系》**：动手 `make` 编译 Git，把本讲 4.1.2 里「版本号在构建期注入」的链路完整跑通，看清 `GIT-VERSION-GEN` 如何生成 `GIT_VERSION` 宏。
2. **接着 u1-l3《源码目录结构地图》、u1-l4《命令分发主入口 git.c》**：建立源码树的整体认知，看懂命令行是怎么被分发到 `builtin/*.c` 的。
3. **想深入「对象数据库」**：本讲 4.2 只讲了概念。真正的对象读写源码（`object-file.c` 里的 `write_object_file`、松散对象与 pack 格式）会在**第 6 单元「对象模型与对象存储」**系统精讲，届时再回看本讲的「内容寻址」流程图，会有豁然开朗之感。
4. **配套阅读**：趁热打铁读 `Documentation/gittutorial.adoc`（用）和 `Documentation/gittutorial-2.adoc`（讲对象数据库与 index 的进阶教程，与本讲 4.2 完美衔接）。

> 记住：本手册的设计是「先讲定位与设计哲学（u1-l1）→ 再讲构建与目录（u1-l2/l3/l4）→ 再讲仓库发现（u2）→ 再讲对象/索引/引用三大子系统（u3-u5）」。不要跳级去啃 C 源码细节，按依赖顺序学，每一步都有前置铺垫。
