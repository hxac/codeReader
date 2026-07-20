# 配置包清单与预设集合

## 1. 本讲目标

在 [u1-l3](u1-l3-running-and-usage.md) 里我们已经用过 `bash rime-install :preset` 这样的命令，也提到 `:preset` / `:extra` / `:all` 是三档「预设集合」，但当时刻意回避了一个问题：**这个 `:preset` 到底是怎么变成一串具体包名的？** 本讲就来回答它。

学完本讲，你应当能够：

- 说清楚 `:preset`、`:extra`、`:all` 三档集合各自包含哪些包，以及 `:all` 是如何由前两者组合而成的；
- 理解 `package_list` 这个 Bash 数组的约定——为什么清单文件用 `+=` 追加、为什么 `all-packages.conf` 要先清空再 `source`；
- 解释 `load_package_list_from_target` 如何把一个 `:preset` 字符串「翻译」成对 `preset-packages.conf` 的 `source`；
- 知道 Windows 侧的 `preset-packages.bat` 等文件如何用 `%package_list%` 提供一套对应的批处理实现。

## 2. 前置知识

本讲默认你已经读过 [u1-l3](u1-l3-running-and-usage.md)，知道以下两点：

1. **target**：命令行上每个空格分隔的参数都是一个 target，例如 `bash rime-install :preset luna-pinyin` 有两个 target。
2. **预设集合**：以冒号开头的 target（`:preset` / `:extra` / `:all`）代表「一组预先定义好的包」。

此外还需要一点点 Bash 知识（不熟也没关系，下面会顺带解释）：

- **数组**：`package_list=(a b c)` 声明一个包含三个元素的数组；`package_list+=(d)` 表示「向数组追加一个元素」；`"${package_list[@]}"` 表示「展开数组的所有元素」。
- **`source`**：在当前 Shell 里执行另一个脚本文件里的代码。关键在于「**当前 Shell**」——被 `source` 的文件里对变量的赋值，会保留在调用方进程里，不会因为子进程退出而消失。这正是清单文件能工作的核心。

还有一个容易混淆的点要先澄清：plum 里的 `*-packages.conf` 文件**不是** INI/JSON/YAML 那种「被解析的配置」，它们本质上是**可执行的 Bash 片段**。这个认识会贯穿整篇讲义。

## 3. 本讲源码地图

本讲涉及的文件分为三组：

| 文件 | 作用 |
| --- | --- |
| [preset-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf) | 定义 `:preset` 集合（8 个基础包），用 `package_list+=(...)` 追加 |
| [extra-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/extra-packages.conf) | 定义 `:extra` 集合（14 个扩展包），同样用 `+=` 追加 |
| [all-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/all-packages.conf) | 定义 `:all` 集合：先清空 `package_list`，再 `source` 前两个文件，得到 22 个包 |
| [preset-packages.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.bat) | Windows 批处理版，用 `set package_list=...` 拼字符串 |
| [extra-packages.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/extra-packages.bat) | `:extra` 的批处理版 |
| [all-packages.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/all-packages.bat) | `:all` 的批处理版：先清空再 `call` 前两者 |
| [scripts/resolver.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh) | `load_package_list_from_target` 负责把 target 归约成 `package_list` |
| [scripts/install-packages.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh) | 消费 `package_list` 的安装主循环 |
| [rime-install.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat) | Windows 安装器，路由 `:preset` 到对应 `.bat` |

## 4. 核心概念与源码讲解

### 4.1 三档预设集合：preset / extra / all

#### 4.1.1 概念说明

plum 维护着 22 个官方包（见 [u1-l1](u1-l1-project-overview.md)）。把它们一次性全装上对大多数用户来说太多了，于是 plum 按「常用程度」把它们切成两档，再用一档把两份合并，共三档：

- **`:preset`（基础集）**：几乎每个 Rime 用户都用得上的核心包，共 **8 个**。
- **`:extra`（扩展集）**：面向特定需求（粤语、双拼、字形、国际音标等）的进阶包，共 **14 个**。
- **`:all`（全集）**：上面两档的并集，共 **22 个**。

三者的关系可以写成集合等式：

\[
\texttt{:all} \;=\; \texttt{:preset} \;\cup\; \texttt{:extra}
\]

也就是说 `:all` 并不是又抄写了一遍 22 个包名，而是「复用」前两份定义拼出来的——这一点在 4.1.3 的源码里会看得很清楚。

#### 4.1.2 核心流程

三档集合的「展开」流程：

1. 用户输入 target，例如 `:preset`。
2. resolver 的 `load_package_list_from_target` 识别出冒号前缀，定位到 `preset-packages.conf`。
3. 在当前 Shell 进程里 `source` 这个文件。
4. 文件里的 `package_list+=( ... )` 把 8 个包名追加进数组。
5. 主循环 `for package in "${package_list[@]}"` 逐个安装。

`:all` 多了一步：它的文件先 `package_list=()` 清空数组，再依次 `source` preset 和 extra 两份文件，于是数组里累积出 22 个元素。

#### 4.1.3 源码精读

先看 `:preset` 的内容——8 个包，按字母序排列：

[preset-packages.conf:L3-L12](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf#L3-L12) —— 用 `package_list+=(...)` 一次性追加 bopomofo、cangjie、essay、luna-pinyin、prelude、quick、stroke、terra-pinyin 这 8 个基础包。

再看 `:extra` 的内容——14 个包，覆盖方言、双拼、字形、IPA 等：

[extra-packages.conf:L3-L18](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/extra-packages.conf#L3-L18) —— 同样用 `+=` 追加 array、cantonese、combo-pinyin、double-pinyin、emoji、ipa、jyutping、middle-chinese、pinyin-simp、scj、soutzoe、stenotype、wubi、wugniu 共 14 个扩展包。

最后是体现「复用」思想的 `:all`：

[all-packages.conf:L3-L6](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/all-packages.conf#L3-L6) —— 第 3 行先 `package_list=()` 把数组清空；第 5、6 行依次 `source` preset 和 extra 两份清单。因为这两份清单都用 `+=` 追加，连续 `source` 之后 `package_list` 里就累加出了 8 + 14 = 22 个包，无需重复抄写包名。

> 小贴士：这里用 `${root_dir:-.}` 而不是写死路径，是为了无论从哪个工作目录调用都能找到清单文件。`root_dir` 由入口脚本 `rime-install` 在 [rime-install:L26](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L26) 通过 `export root_dir="${plum_dir}"` 注入。

#### 4.1.4 代码实践

**实践目标**：验证 `:all` 确实等于 `:preset` 与 `:extra` 的并集，且不重复、不遗漏。

**操作步骤**（纯源码阅读型，不需要联网）：

1. 打开 [preset-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf)，数出 `package_list+=(...)` 里的包名个数，记为 \(n_p\)。
2. 打开 [extra-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/extra-packages.conf)，数出包名个数，记为 \(n_e\)。
3. 打开 [all-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/all-packages.conf)，确认它只是 `source` 了上面两份文件、没有再写任何包名。

**需要观察的现象**：`all-packages.conf` 里除了清空和两条 `source` 之外，没有任何硬编码的包名。

**预期结果**：\(n_p = 8\)、\(n_e = 14\)、\(n_p + n_e = 22\)，与 [u1-l1](u1-l1-project-overview.md) 中「官方包共 22 个」的说法一致。若想把并集真的跑出来，可在仓库根目录执行（需要 Bash）：

```bash
package_list=()
source ./preset-packages.conf
source ./extra-packages.conf
echo "共 ${#package_list[@]} 个包：${package_list[*]}"
```

#### 4.1.5 小练习与答案

**练习 1**：如果要在 `:preset` 里新增一个包（比如 `pinyin-simp`），需要改几个文件才能让 `:all` 也自动包含它？

**答案**：只改 [preset-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf) 一处即可。因为 `all-packages.conf` 是通过 `source preset-packages.conf` 复用其内容的，preset 一变，all 自动跟着变——这正是「集合等式」带来的维护便利。

**练习 2**：`:extra` 里的 `emoji` 包为什么没有出现在 `:preset` 里？从产品定位角度给出一个合理解释。

**答案**：`:preset` 收录的是「几乎所有用户都需要」的最小核心（基础前置 prelude、词频表 essay、主流拼音/注音/字形方案）。emoji 属于「锦上添花」的扩展资源，并非输入中文所必需，因此归入 `:extra`，由用户按需选择。这体现了 preset/extra 两档按「必要 vs 可选」划分的设计意图。

---

### 4.2 `package_list` 数组约定与 source 加载机制

#### 4.2.1 概念说明

上一节我们看到，三份 `.conf` 文件都在操作同一个名字：`package_list`。这不是巧合，而是 plum 的一条**全局约定**：

> 任何一份「包清单」文件，其唯一职责就是往名为 `package_list` 的数组里填入包名。

这条约定之所以能成立，依赖两个机制：

1. **`source` 的「同进程」语义**：清单文件不是被当成数据解析，而是被 `source` 进 resolver 所在的 Shell 进程，它对 `package_list` 的赋值直接留在当前进程里。
2. **`+=` 追加**：清单文件统一用 `package_list+=(...)` 而不是 `package_list=(...)`。`+=` 表示「在已有内容后面追加」，这使得「一份清单 = 多份子清单拼接」（比如 `:all`）成为可能。

理解了这条约定，你就掌握了 plum 清单系统的全部「语法」——后面要自定义集合，只要照着写就行。

#### 4.2.2 核心流程

resolver 中负责把 target 翻译成 `package_list` 的函数是 `load_package_list_from_target`。它用一个 `case` 语句把四类 target 分别处理：

```
load_package_list_from_target(target):
    case target:
        远程 *-packages.conf URL   → curl 下载后 source
        本地 *.conf 文件           → 直接 source
        :name 形式的预设集合       → source "${root_dir}/${name}-packages.conf"
        其它（裸包名等）           → package_list=("${target}")   # 单元素数组
```

本讲关注第三条（`:name`）。注意它的巧妙之处：**冒号前缀的集合名直接被拼成了文件名**——`:preset` → `preset-packages.conf`、`:extra` → `extra-packages.conf`、`:all` → `all-packages.conf`。这是一种「按命名约定路由」的设计：要新增一档集合 `:my`，只要放一个 `my-packages.conf` 文件即可，无需改任何代码。

#### 4.2.3 源码精读

先看 resolver 里的 `case` 分支：

[scripts/resolver.sh:L70-L95](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L70-L95) —— `load_package_list_from_target` 的全部逻辑。其中第 88–90 行专门处理 `:preset` 这类冒号前缀 target。

[scripts/resolver.sh:L88-L90](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L88-L90) —— `${target#:}` 用参数展开剥掉开头的冒号得到 `preset`，再拼成 `${root_dir:-.}/preset-packages.conf` 去 `source`。一句话就完成了「集合名 → 清单文件」的映射。

清单填好 `package_list` 之后，由主循环消费：

[scripts/install-packages.sh:L114-L118](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L114-L118) —— 第 114 行调用 `load_package_list_from_target` 把 target 归约成 `package_list`；第 116–118 行用 `for ... in "${package_list[@]}"` 遍历数组，逐个调用 `install_package`。

> 为什么 `all-packages.conf` 必须先 `package_list=()` 再 source？看 [install-packages.sh:L114](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/install-packages.sh#L114) 上下文会发现：主循环并没有事先初始化 `package_list`。虽然 `rime-install` 每个 target 都会**新起一个 `install-packages.sh` 子进程**（见 [rime-install:L60](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L60)），进程初始时 `package_list` 本就不存在，preset/extra 用 `+=` 也能凭空建出数组；但 `all-packages.conf` 要在**同一个进程内**连续 `source` 两份都用 `+=` 的清单，显式清空是一种防御性写法——保证即便有人把 `all-packages.conf` 嵌进别的、`package_list` 已有内容的脚本里，结果也依然是「纯净的 22 个包」而非累加脏数据。

#### 4.2.4 代码实践

**实践目标**：亲手创建一份自定义集合 `my-packages.conf`，验证它能被 resolver 通过 `source` 正确加载成 `package_list`。

**操作步骤**：

1. 在仓库根目录新建文件 `my-packages.conf`，内容照抄官方约定（注意用 `+=`）：

   ```bash
   #!/usr/bin/env bash

   package_list+=(
       luna-pinyin
       emoji
   )
   ```

2. **方式 A——直接 source 验证**（最干净，推荐先做）：

   ```bash
   cd /path/to/plum   # 进入仓库根目录
   package_list=()    # 模拟 install-packages.sh 的初始状态
   source ./my-packages.conf
   echo "加载到 ${#package_list[@]} 个包：${package_list[*]}"
   ```

3. **方式 B——走真实安装链路**（不实际联网安装，只验证 target 能被识别）：

   ```bash
   # .conf 文件会命中 resolver 的 *.conf 分支（本地清单），同样走 source
   bash scripts/install-packages.sh ./my-packages.conf ./rime-test
   ```

   或把它当成本地清单传给入口：

   ```bash
   rime_dir=./rime-test bash rime-install my-packages.conf
   ```

**需要观察的现象**：方式 A 应打印 `加载到 2 个包：luna-pinyin emoji`；方式 B 会进入正常的安装流程（开始 clone `rime/rime-luna-pinyin` 等），说明 `my-packages.conf` 被当成了合法 target。

**预期结果**：`package_list` 内容与你写入的包名一致，证明「清单文件 = 往 `package_list` 追加的 Bash 片段」这条约定成立。

> 说明：方式 B 会真正发起网络请求去 clone 包。如果你只想验证「target 被识别、清单被加载」，看到开始下载即可中断（`Ctrl+C`），无需等它装完——待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `my-packages.conf` 里的 `package_list+=( ... )` 改成 `package_list=( ... )`（去掉 `+`），单独运行方式 A 时结果有区别吗？在什么场景下才会有区别？

**答案**：单独运行方式 A 时（之前刚 `package_list=()` 清空过），`=` 和 `+=` 结果一样，都是那 2 个包。区别只在「`package_list` 已有内容」时才显现：`=` 会**覆盖**掉原有内容，`+=` 则**追加**。所以官方清单统一用 `+=`，正是为了让 `all-packages.conf` 能把 preset 和 extra 拼到一起而不互相覆盖。

**练习 2**：为什么 resolver 处理 `:preset` 时用的是 `${target#:}`（剥冒号）而不是 `${target#:}` 之后再查一张「集合名 → 文件名」的映射表？

**答案**：因为 plum 采用了「**按命名约定路由**」：集合名直接对应 `<name>-packages.conf` 文件名，不需要额外的映射表。好处是**零代码扩展**——用户放一个 `foo-packages.conf` 就能立刻用 `:foo`，无需修改 resolver。代价是集合名必须和文件名严格对应，不能取别名。

---

### 4.3 Windows 侧的 `.bat` 双实现

#### 4.3.1 概念说明

plum 的主实现是 Bash，但在 Windows 上原生 Shell 是 `cmd.exe`，它既不认 `source`、也没有 Bash 数组。为了让 Windows 用户也能开箱即用，plum 为每份 `.conf` 清单都准备了一个**同名的 `.bat` 文件**，构成一套「双实现」：

| Bash 实现 | Windows 实现 | 对应集合 |
| --- | --- | --- |
| `preset-packages.conf` | `preset-packages.bat` | `:preset` |
| `extra-packages.conf` | `extra-packages.bat` | `:extra` |
| `all-packages.conf` | `all-packages.bat` | `:all` |

两套实现的**职责完全相同**——都是把一组包名填进一个名为 `package_list` 的变量；区别只在「用什么语言机制」：Bash 用数组 + `source`，批处理用空格分隔的字符串 + `call`。

> 名词解释：
> - **`cmd.exe`**：Windows 默认命令行解释器，执行 `.bat`/`.cmd` 文件。
> - **`call`**：批处理里「在当前批处理上下文中执行另一个 `.bat`」的命令，作用类似 Bash 的 `source`——被 `call` 的脚本里 `set` 的变量会保留下来。
> - **`set package_list=...`**：批处理给变量赋值。批处理没有数组，所谓「列表」就是用一个空格分隔的长字符串，后续用 `for %%p in (%package_list%)` 逐个取出。

#### 4.3.2 核心流程

Windows 侧的展开流程，与 Bash 版一一对应：

1. 用户运行 `rime-install.bat :preset`。
2. 批处理安装器在参数路由里识别出冒号前缀，把 `:preset` 变成 `call preset-packages.bat`。
3. `preset-packages.bat` 执行 `set package_list=%package_list% bopomofo ...`，把 8 个包名拼进字符串。
4. 回到安装器，执行 `:install_package_group`，用 `for %%p in (%package_list%)` 遍历每个包名并安装。

`:all` 同样是「先清空再 call 两份」：`set package_list=`（赋空）→ `call preset-packages.bat` → `call extra-packages.bat`。

#### 4.3.3 源码精读

先看 `:preset` 的批处理版——注意它如何用 `^`（续行符）把一长串包名拆到多行：

[preset-packages.bat:L1-L9](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.bat#L1-L9) —— `set package_list=%package_list%^` 开头先把「已有内容 + 一个空格」放上，随后每行 ` 包名^` 追加一个包名（行首的空格是分隔符，行尾的 `^` 表示「下一行还是同一条命令」）。最终 `package_list` 是一个空格分隔的字符串，含 8 个包名，与 `preset-packages.conf` 内容一致。

对比 Bash 版用数组 `+=( ... )`，批处理用「字符串拼接」达到同等效果——这是两套实现的本质差异。

再看 `:all` 的批处理版，结构与 Bash 版高度对称：

[all-packages.bat:L1-L3](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/all-packages.bat#L1-L3) —— 第 1 行 `set package_list=` 把变量清空（注意等号后什么都没有）；第 2、3 行 `call preset-packages.bat` 和 `call extra-packages.bat`。因为这两个子文件都用 `set package_list=%package_list% ...`（把旧值带上再追加），连续 `call` 之后 `%package_list%` 就累积成了 22 个包名。

最后看安装器如何把 `:preset` 路由到 `.bat` 文件：

[rime-install.bat:L100-L105](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L100-L105) —— 第 100–102 行处理「显式给出 `xxx-packages.bat` 文件名」的 target；第 103–105 行处理冒号前缀：`"%package::=%"` 把 `:preset` 里的冒号去掉得到 `preset`，拼成 `preset-packages.bat` 去 `call`。这与 Bash 侧 resolver 的 `${target#:}` 思路完全一致——都是「剥掉冒号、拼上 `-packages` 后缀」。

call 完之后，由 `:install_package_group` 消费 `%package_list%`：

[rime-install.bat:L199-L209](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L199-L209) —— 第 200 行先检查 `package_list` 是否已定义（未定义就报错，对应 Bash 侧 `package_list` 为空的情形）；第 204 行 `for %%p in (%package_list%) do ...` 把空格分隔的字符串拆成一个个包名，逐个 `call :install_package`。这等价于 Bash 侧 [install-packages.sh:L116-L118](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L116-L118) 的 `for package in "${package_list[@]}"`。

> 补充：当 Windows 上装了 git-bash 时，`rime-install.bat` 会通过 `:install_with_plum` 把工作**转调**给 Bash 版的 `rime-install`（见 [rime-install.bat:L211-L218](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L211-L218)）。也就是说，`.bat` 清单主要服务于「纯 Windows、无 git-bash」的离线/ZIP 安装场景；一旦有 Bash 环境，两套实现会汇合到同一套 Bash 逻辑上。这部分细节属于 [u3-l2](u3-l2-windows-batch-installer.md) 的范围，本讲只点到为止。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码，理清 `.bat` 版「冒号集合 → 包名字符串」的路由与消费链路，不实际运行。

**操作步骤**（源码阅读型）：

1. 打开 [rime-install.bat:L103-L105](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L103-L105)，确认 `:preset` 是如何被去掉冒号、拼成 `preset-packages.bat` 并 `call` 的。
2. 打开 [preset-packages.bat:L1-L9](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.bat#L1-L9)，数出其中包名个数，与 [preset-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf) 对照。
3. 打开 [rime-install.bat:L199-L209](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L199-L209)，看清 `for %%p in (%package_list%)` 如何把字符串拆开逐个安装。

**需要观察的现象**：`.bat` 与 `.conf` 两份文件里的包名集合、顺序完全一致；批处理用字符串 + `for` 模拟了 Bash 的数组 + `for`。

**预期结果**：能用自己的话讲清楚——在 Windows 上，`rime-install.bat :preset` 一条命令，依次经过了「剥冒号 → 拼 `.bat` 文件名 → `call` → `set package_list` → `for %%p in (%package_list%)` → `:install_package`」这条链路。

#### 4.3.5 小练习与答案

**练习 1**：`.bat` 文件每行末尾的 `^` 起什么作用？如果去掉会怎样？

**答案**：`^` 是 cmd.exe 的**续行符**，表示「下一行仍属于当前命令」。`set package_list=...` 因为要罗列 8 个包名，写成一行太长难读，于是用 `^` 把它折成多行。如果去掉 `^`，cmd 会把每一行当作独立的命令，第一个 `set` 只会拿到第一个包名，后续行会被当成无效命令而报错或忽略，`package_list` 就不全了。

**练习 2**：Bash 版用数组 `package_list=(a b c)`，批处理版用空格分隔字符串 `set package_list=a b c`。这两种表示在「包名本身含有空格」时各会怎样？

**答案**：Bash 数组天然支持含空格的元素（`package_list=("a b" c)`），`"${package_list[@]}"` 能正确还原。而批处理的 `for %%p in (%package_list%)` 默认按空格/分号切分，**无法**表达含空格的包名。不过这在实际中不是问题——GitHub 仓库名不允许含空格，所以用空格做分隔符是安全的简化。

## 5. 综合实践

把本讲三个最小模块串起来，做一个「**自定义一档集合，并让它跨平台可用**」的小任务。

**背景**：假设你要给团队维护一份内部推荐集合 `:recommended`，包含 `prelude`、`essay`、`luna-pinyin`、`terra-pinyin` 四个包，并且希望 Linux/macOS 和 Windows 用户都能用 `:recommended` 这一档。

**任务**：

1. **写 Bash 版清单**：在仓库根目录新建 `recommended-packages.conf`，仿照 [preset-packages.conf](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf) 的写法，用 `package_list+=(...)` 写入这 4 个包名。

2. **验证 Bash 版**：

   ```bash
   package_list=()
   source ./recommended-packages.conf
   echo "${#package_list[@]}: ${package_list[*]}"
   ```

   预期输出 `4: prelude essay luna-pinyin terra-pinyin`。再想一下：为什么不需要改 resolver 就能让 `:recommended` 直接可用？（答：因为 [resolver.sh:L88-L90](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L88-L90) 按命名约定 `source "${root_dir}/recommended-packages.conf"`。）

3. **写 Windows 版清单**：再新建一个 `recommended-packages.bat`，仿照 [preset-packages.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.bat)，用 `set package_list=%package_list%^ ...` 写入同样 4 个包名（注意每行末尾的 `^`、行首留一个空格）。

4. **反思**：对照两份文件，总结「同一份集合、两种语言」各用了什么机制填 `package_list`（Bash：数组 + `+=` + `source`；批处理：字符串拼接 + `call`）。这正好是本讲 4.2 与 4.3 的浓缩。

> 注意：步骤 3 的 `.bat` 文件若没有 Windows 环境可跳过实际运行，重点是体会「双实现」的对应关系——待本地验证。

## 6. 本讲小结

- plum 维护三档预设集合：`:preset`（8 个基础包）、`:extra`（14 个扩展包）、`:all`（前两者的并集，共 22 个），三者关系满足 \(\texttt{:all} = \texttt{:preset} \cup \texttt{:extra}\)。
- 清单文件（`*-packages.conf`）**不是被解析的配置**，而是**可执行的 Bash 片段**，唯一职责是用 `package_list+=(...)` 往全局数组里追加包名；统一用 `+=` 是为了让多份清单能拼接（如 `:all`）。
- resolver 的 `load_package_list_from_target` 对 `:name` 形式的 target，通过 `${target#:}` 剥掉冒号、拼成 `<name>-packages.conf` 去 `source`——这是一种「按命名约定路由、零代码扩展」的设计。
- `all-packages.conf` 的 `package_list=()` 先清空再 source，是防御性写法，保证无论在什么上下文都能得到纯净的 22 个包。
- Windows 侧用同名 `.bat` 文件提供对应实现：用 `set package_list=...`（空格分隔字符串）替代 Bash 数组，用 `call` 替代 `source`，由 `rime-install.bat` 的路由逻辑把 `:preset` 转成 `call preset-packages.bat`。
- 两套实现的消费端也对称：Bash 用 `for ... in "${package_list[@]}"`，批处理用 `for %%p in (%package_list%)`。

## 7. 下一步学习建议

- 想了解这些清单被加载之后，**每个包具体是怎么被下载、更新、拷贝**的，继续读 [u2-l3 安装主循环 install-packages.sh](u2-l3-install-main-loop.md) 和 [u2-l5 包的拉取与更新](u2-l5-fetch-and-update-package.md)。
- 想看 `--select` 模式如何让人**交互式地从预设集合里勾选**包，读 [u3-l1 交互式包选择 selector.sh](u3-l1-interactive-selector.md)。
- 想深入 Windows 那套「先尝试 ZIP/7z，检测到 git-bash 再转调 Bash」的完整路由，读 [u3-l2 Windows 批处理安装器](u3-l2-windows-batch-installer.md)。
- 动手建议：结合本讲的「自定义集合」实践，尝试写一份 `my-packages.conf` 并通过 `bash rime-install my-packages.conf` 真实安装一次，把本讲与 [u1-l3](u1-l3-running-and-usage.md) 的命令行用法打通。
