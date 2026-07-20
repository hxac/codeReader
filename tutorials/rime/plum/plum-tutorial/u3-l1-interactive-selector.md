# 交互式包选择 selector.sh

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚在 `rime-install` 中 `--select` 标志被处理的**完整时序**：它何时被识别、如何改变后续参数流。
- 读懂 `scripts/selector.sh` 中 `select_packages` 的两段结构：先把候选 target 展开去重成菜单、再用 bash 内建 `select` 与 `case` 处理用户的各种输入。
- 解释菜单中 `reset`、`.`、`end`、`cancel`、手输包名等指令分别触发什么行为，以及**空选回退**到默认包的确认逻辑。

本讲是「扩展、跨平台与工程化」单元的第一篇。前面 u2 系列已经把「配方字符串怎么解析」「包怎么下载安装」讲透了；本讲回到用户体验层面，看 plum 如何让用户**在不记包名**的情况下挑包安装。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义），这里只做最简回顾：

- **target 与 package_list**（u1-l3、u2-l2、u2-l7）：`rime-install` 命令行每个空格分隔的参数叫一个 target；`:preset`/`:extra`/`:all` 这类冒号开头的 target 会被 `load_package_list_from_target` source 对应的 `*-packages.conf`，展开成 `package_list` 数组里的若干裸包名。
- **require/provide 模块机制**（u2-l1）：`require 'selector'` 会按需 `source` `scripts/selector.sh`，文件末尾的 `provide 'selector'` 负责登记去重。
- **styles.sh 的输出函数**（u2-l1）：`highlight`/`warning`/`error`/`prompt`/`print_item`/`print_option`/`print_result` 都只是「起始色 + `$@` + reset」的包装，本讲会大量看到它们。

你还需要一点关于 bash 内建 `select` 的知识，本讲会在 4.2 里专门补上。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `rime-install` | 62 行 | 总入口。负责识别 `--select`、决定候选 target、在交互模式下调用 `select_packages` 并用其结果替换 target 列表。 |
| `scripts/selector.sh` | 69 行 | 定义唯一的函数 `select_packages`：构建菜单、处理用户输入、空选回退。 |

调用关系非常简单：

```
rime-install
   └─ (interactive=1 时) require 'selector'
        └─ select_packages "${targets[@]}"   # 读写全局数组 selected_packages
   └─ targets=("${selected_packages[@]}")     # 用选择结果覆盖候选
   └─ for target in targets; install-packages.sh ...
```

selector.sh 自身又 `require 'styles'`（输出配色）和 `require 'resolver'`（用 `load_package_list_from_target` 展开 target）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应规格里的三项：**--select 入口处理**、**select 菜单与指令**、**空选回退**。

### 4.1 `--select` 入口处理

#### 4.1.1 概念说明

正常模式下，`rime-install` 把命令行参数原样当作 target 逐个安装，用户必须**事先知道**要装哪些包。`--select` 是一个**模式开关**：把它作为第一个参数，plum 就进入交互模式——先把后续 target 展开成一个**候选菜单**，让用户在里面挑，挑中的才真正安装。

这里有一个关键设计：`--select` 改变的不是「装什么」的最终结果，而是**在结果确定前插入一道人工选择工序**。候选池仍来自命令行后续参数（`:preset`、`:extra`、具体包名等），只是多了一步「人肉过滤」。

#### 4.1.2 核心流程

入口处的处理时序如下（伪代码）：

```
1. 若第一个参数是 --select：
       shift 去掉它；置 interactive=1
2. 计算候选 targets：
       若此刻已无参数  → targets=(':preset')   # 交互模式下也有默认候选
       否则            → targets=("$@")
3. 若 interactive：
       require 'selector'
       select_packages "${targets[@]}"          # 展开 + 菜单，结果写全局 selected_packages
       targets=("${selected_packages[@]}")      # 用选择结果覆盖 targets
4. for target in targets: 交给 install-packages.sh
```

注意步骤 2 的细节：即便用户写了 `rime-install --select`（后面什么都没跟），`targets` 也会落成 `(':preset')` 而不是空。这保证了菜单里**总有东西可选**。

#### 4.1.3 源码精读

`--select` 的识别发生在 [rime-install:36-39](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L36-L39)，它只认**第一个参数**（`$1`），匹配上就 `shift` 掉并置 `interactive=1`：

```bash
if [[ "$1" == '--select' ]]; then
    shift
    interactive=1
fi
```

紧接着 [rime-install:41-45](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L41-L45) 计算 `targets`。因为上一步已经 `shift`，这里的 `$#` 反映的是**去掉 `--select` 之后**的参数个数：

```bash
if [[ $# -eq 0 ]]; then
    targets=(':preset')
else
    targets=("$@")
fi
```

真正派发到 selector 的是 [rime-install:47-51](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L47-L51)：

```bash
if [[ -n "${interactive}" ]]; then
    require 'selector'
    select_packages "${targets[@]}"
    targets=("${selected_packages[@]}")
fi
```

这里有两个要点：

1. `select_packages` 是**在当前进程**里直接调用的（不是子 shell）。这非常关键——`selected_packages` 是函数内对**全局数组**的赋值，只有在同一进程里，下一行 `targets=("${selected_packages[@]}")` 才读得到。如果误写成 `$(select_packages ...)`，选择结果会随子 shell 一起丢失。
2. 无论交互模式下用户挑了什么，最终都统一回到 [rime-install:53-61](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L53-L61) 的主循环，和正常模式走同一条 `install-packages.sh` 安装链路。`--select` 没有引入新的安装逻辑，只改了 `targets` 的内容。

#### 4.1.4 代码实践

**实践目标**：确认 `--select` 必须是第一个参数，且它只起「开关 + shift」作用。

**操作步骤**：

1. 阅读上面引用的 4 行代码。
2. 思考：如果用户写 `bash rime-install :preset --select`（`--select` 在第二位），会发生什么？
3. 用一段话回答：此刻 `$1` 是什么？`interactive` 会被置 1 吗？`:preset` 和 `--select` 最终会被分别当成什么 target？

**预期结果**（可由代码直接推断，无需运行）：

- `$1` 是 `:preset`，不等于 `'--select'`，故 `interactive` 保持未设置。
- `targets=(":preset" "--select")`，两个字符串都成了 target。
- 主循环里 `:preset` 正常安装 8 个预设包；`--select` 这个字符串既不是 `plum`，也不被 resolver 的任何 `case` 分支匹配，会落到 `load_package_list_from_target` 的 `*)` 默认分支，被当成裸包名 `--select` 处理（多半下载失败）。

**需要观察的现象**：`--select` 的识别是**位置敏感**的，这是「模式开关」与「普通参数」的区别。

> 待本地验证：在空目录 `rime_dir=./rime-test bash rime-install :preset --select` 跑一次，确认 `--select` 被当作包名处理（预期报找不到仓库）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--select` 的判断放在「猜 `rime_dir`」（第 30–34 行）之后、而不是脚本最开头？

**参考答案**：因为 `--select` 只关心参数解析，与「装到哪」无关；而 `rime_dir` 的猜测（`guess_rime_user_dir`）越早完成，后续所有路径就越确定。两者互不依赖，plum 选择先稳定环境变量、再处理参数语义。代码上把它们写成两个独立 `if`，顺序可互换但当前顺序更符合「先环境、后参数」的直觉。

**练习 2**：`interactive` 变量从未用 `local` 声明，会有问题吗？

**参考答案**：不会有问题，反而正合适。`rime-install` 是顶层脚本，没有外层调用者会被这个全局变量污染；而它需要跨多个 `if` 块传递「是否交互」的状态，用全局（默认）作用域是最直接的写法。

---

### 4.2 `select` 菜单与指令

#### 4.2.1 概念说明

这是本讲的重头戏。`select_packages` 函数分两段：**第一段**把命令行传进来的 target 全部展开、去重，得到一个**候选包数组** `all_packages`；**第二段**用 bash 内建 `select` 把这个数组渲染成编号菜单，然后循环读取用户输入。

先补一下 `select` 这个 bash 内建命令的背景（很多初学者没接触过）：

```
select 名称 in 词1 词2 ...; do
    # 每次用户输入后执行一次
done
```

它的行为是：

- 自动把 `in` 后面的词编号（从 1 开始）打印成菜单。
- 用变量 `PS3` 作为提示符，等待用户从标准输入读一行。
- 把用户**输入的原始文本**存入 `REPLY`；把该编号对应的词存入 `名称`。
- 如果输入的不是有效编号，`名称` 为空，但 `REPLY` 仍保留原始输入——这正是 plum 用来识别「指令」和「手输包名」的入口。

所以 `select` 天然给了我们两份信息：**选了第几项**（`名称`）和**到底敲了什么**（`REPLY`）。plum 同时利用了这两份信息。

#### 4.2.2 核心流程

`select_packages` 的整体流程：

```
# 第一段：构建候选
all_packages = ()
for target in "$@":                         # 遍历每个 target
    load_package_list_from_target(target)    # 展开成 package_list
    for package in package_list:
        if package 不在 all_packages 中:     # 去重
            all_packages += (package)

# 第二段：交互
selected_packages = ()
设置 PS3 提示符
select selected in all_packages:            # 渲染菜单
    若 selected 非空（选了有效编号）:
        selected_packages += (selected)
    否则按 REPLY 分支:
        end|ok|0|.   → break                 # 结束选择
        cancel|exit|quit → 警告 + exit       # 整个安装中止
        reset|clear  → 清空已选 + 继续       # 重新挑
        [:A-Za-z]*   → 把 REPLY 当包名加入   # 手输
        *            → 报错 + 继续           # 无效输入
    （未 break/exit/continue 时）打印当前已选状态
```

#### 4.2.3 源码精读

**第一段：展开与去重**，见 [selector.sh:6-17](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L6-L17)：

```bash
select_packages() {
    local all_packages=()
    local target
    local package
    for target in "$@"; do
        load_package_list_from_target "${target}"
        for package in "${package_list[@]}"; do
            if ! (echo " ${all_packages[*]} " | grep -qF " ${package} "); then
                all_packages+=("${package}")
            fi
        done
    done
```

注意 [selector.sh:13](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L13) 的去重技巧——它和 u2-l1 里 `bootstrap.sh` 判断 `loaded_modules` 用的是同一招：

```bash
if ! (echo " ${all_packages[*]} " | grep -qF " ${package} "); then
```

把数组展开成空格分隔的串，**首尾各补一个空格**，再用 `grep -qF " ${package} "`（注意查询串两侧也有空格，`-F` 表示按字面字符串而非正则）做整词匹配。两侧补空格是为了把「子串误命中」升级成「整词命中」——否则像 `essay` 会误匹配到 `essayhts`（假设存在）这种超名上。`-q` 让 grep 静默、只用返回码表达「在/不在」，外层 `!` 取反：不在就追加。

> 小贴士：`all_packages`、`target`、`package` 都声明成了 `local`，但下面你会看到 `selected_packages` **没有** `local`——这是刻意的，它要作为函数向调用者回传结果的「出口」。

**第二段：菜单与指令分发**，见 [selector.sh:19-49](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L19-L49)。先设提示符并启动 `select`：

```bash
selected_packages=()
local PS3="$(prompt '#') Enter number, package name or '.' when finished $(prompt '#') "
echo $(highlight 'Select packages to install:')
select selected in "${all_packages[@]}"; do
```

[selector.sh:20](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L20) 把 `PS3` 设成带颜色的提示语；注意它用 `local PS3=`，因为 `PS3` 是 bash 的**特殊变量**，全局改它会污染调用者，局部化是礼貌做法。提示语已经把三种合法输入都告诉用户了：编号、包名、`.` 表示完成。

接着是 [selector.sh:23-24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L23-L24) 的「有效编号」分支：

```bash
if [[ -n "${selected}" ]]; then
    selected_packages+=("${selected}")
```

只要 `selected` 非空（即用户输入了一个有效编号），就直接把它追加进已选列表——这是最常见的情况。

否则进入 [selector.sh:25-47](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L25-L47) 的 `case "$REPLY"`，按原始输入分派。各分支逐一看：

```bash
end | ok | 0 | .)
    break
    ;;
```

[selector.sh:27-29](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L27-L29)：四种「完成」写法都 `break` 退出 `select` 循环，带着当前已选的 `selected_packages` 进入下一阶段。`0` 也算完成（即使菜单编号从 1 开始，输 0 不是有效编号，这里正好复用）。

```bash
cancel | exit | quit)
    echo $(warning 'Installation canceled.')
    exit
    ;;
```

[selector.sh:30-33](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L30-L33)：三种「取消」写法直接 `exit`，**终止整个脚本**——不只是退出菜单，而是连后续安装也不做。这与 `end` 的「体面结束」形成对比。

```bash
reset | clear)
    selected_packages=()
    echo $(print_result 'Reset selected packages.')
    continue
    ;;
```

[selector.sh:34-38](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L34-L38)：把已选列表**清空**回 `()`，打印提示后 `continue` 重新显示菜单。注意它清的是 `selected_packages`（已选），不是 `all_packages`（候选）——菜单本身不变。

```bash
[:A-Za-z]*)
    selected_packages+=("$REPLY")
    ;;
```

[selector.sh:39-41](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L39-L41)：这是「手输包名」分支。`[:A-Za-z]*` 是 `case` 的 glob 模式，`[...]` 是字符集，匹配**以冒号、大写或小写字母开头**的任意字符串。所以用户可以直接敲 `luna-pinyin`（字母开头）加入已选，甚至敲 `:preset`（冒号开头）把整个预设当一个条目加进去。匹配上就把 `$REPLY` 原样追加。

```bash
*)
    echo $(error 'ERROR:') 'invalid number or package name:' $(print_option "$REPLY")
    continue
    ;;
```

[selector.sh:42-45](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L42-L45)：兜底分支——比如输了个 `123abc` 或 `-5` 这种既不是编号、也不以字母/冒号开头的串，打印红色错误后 `continue` 重新提示。

最后，循环体末尾 [selector.sh:48](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L48) 是一行**状态回显**：

```bash
echo "You will rime with $(print_item ${selected_packages[@]}) (+$(print_option $REPLY))"
```

每次成功加入一个包后，打印当前已选全集和本次新增项（`$REPLY`）。注意它**只在正常路径**执行：`break`/`exit` 跳出循环、`continue` 重跳到下一轮，都不会跑到这行。所以「加入有效编号」和「手输包名」两种情况才会看到这行回显。

#### 4.2.4 代码实践

**实践目标**：亲手体验菜单，并验证「输入编号」与「手输包名」走的是同一条「加入已选」路径，而 `reset` 只清已选不清候选。

**操作步骤**：

1. 准备一个空目录当 Rime 用户目录，避免污染真实配置：
   ```sh
   mkdir -p /tmp/rime-select-test
   cd /path/to/plum        # 进入 plum 仓库根目录
   ```
2. 运行交互安装（设 `rime_dir` 指向上面的空目录）：
   ```sh
   rime_dir=/tmp/rime-select-test bash rime-install --select :preset
   ```
3. 屏幕上会先看到 `Select packages to install:` 和一张编号菜单（`:preset` 展开后的 8 个包：prelude、essay、luna-pinyin、terra-pinyin、bopomofo、stroke、cangjie、quick）。
4. 依次尝试以下输入，每次观察输出：
   - 输入 `1` 回车（选 prelude）→ 应看到 `You will rime with prelude (+1)`。
   - 输入 `essay` 回车（手输包名）→ 因为 essay 已是菜单项，但仍会被当作手输包名追加（注意：它不会被去重，可能重复出现）。
   - 输入 `reset` 回车 → 应看到黄色的 `Reset selected packages.`，且**菜单再次原样出现**（候选没变）。
   - 输入 `.` 回车 → 菜单结束。
5. 若 `.` 时已选为空，会触发 4.3 的空选回退提示，按 `n` 取消即可，不会写入任何文件。

**需要观察的现象**：

- 菜单**编号**和**包名**都能加入已选。
- `reset` 之后菜单内容不变，证明它只动 `selected_packages` 不动 `all_packages`。
- 状态行 `(+$REPLY)` 里的 `+` 部分：输编号时是数字，输包名时是包名本身。

**预期结果**：选中的包最终被拷进 `/tmp/rime-select-test`；之后可 `rm -rf /tmp/rime-select-test` 清理。

> 待本地验证：本实践依赖联网克隆 GitHub 仓库，若在离线环境运行，菜单仍能看到（来自 `*-packages.conf` 的本地展开），但确认安装那一步会因网络失败。

#### 4.2.5 小练习与答案

**练习 1**：`end | ok | 0 | .` 里的 `0` 为什么和 `end` 等价？菜单编号不是从 1 开始吗？

**参考答案**：正因为菜单编号从 1 开始，输 `0` 不是任何条目的有效编号，于是 `select` 把 `selected` 置空、`REPLY` 赋为 `"0"`，落入 `case` 分支。plum 在 [selector.sh:27](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L27) 主动把 `0` 列为「完成」的同义词，既符合直觉（0 = 无 = 不选了），又复用了「无效编号进 case」这条现成通路。

**练习 2**：如果用户在菜单里手输一个**已经在 `selected_packages` 里的包**（比如重复输 `prelude`），会发生什么？这和第一段的去重矛盾吗？

**参考答案**：会**重复加入**，`selected_packages` 里出现两个 `prelude`。这**不矛盾**：第一段的去重作用在「候选菜单 `all_packages`」上，保证菜单里每个包只列一次；而第二段的「手输/选编号」是对 `selected_packages` 的纯追加，没有任何去重。最终安装时多出的同名条目会由下游 `install-packages.sh` 的增量拷贝逻辑（`diff` 判断内容相同则跳过）兜底，不会真的装两遍。

**练习 3**：`[:A-Za-z]*` 这个模式会把 `cancel`、`exit`、`quit` 误吃进「手输包名」分支吗？为什么？

**参考答案**：不会。`case` 是**按顺序、首次匹配即终止**的。`cancel | exit | quit` 这条分支写在 `[:A-Za-z]*` **之前**（[selector.sh:30](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L30) 早于 [selector.sh:39](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L39)），所以这些保留词先被更具体的分支接走。`[:A-Za-z]*` 只会吃到「没被前面任何分支命中、且以字母/冒号开头」的串。这也是 `case` 书写时「特例在前、通例在后」的典型惯例。

---

### 4.3 空选回退

#### 4.3.1 概念说明

`select` 循环结束后（用户敲了 `.` 或 `end`），可能出现一种尴尬情况：**用户一个包都没选**。可能是误操作，可能是改主意。plum 不直接以空列表收场（那样后续什么都不会装），而是**礼貌地追问一次**：要不要干脆装「默认包」？

这里「默认包」的含义需要精确：它指的不是某个固定清单，而是**最初传给 `select_packages` 的那些 target**（即调用方 `rime-install` 里的 `targets`，通常是 `:preset`）。换句话说，如果用户在菜单里啥都没挑，plum 提供「一键回到非交互结果」的退路。

#### 4.3.2 核心流程

```
select 循环结束后:
if selected_packages 为空:
    打印 "你没有选任何包"
    打印 "是否安装默认包？" 并显示 $@ （原始 target 列表）
    read 读用户回答
    case 回答:
        '' 或 y* → selected_packages=("$@")   # 用原始 target 兜底
        其他     → 警告 + exit                  # 取消整个安装
```

注意两个边界：

- 回答**默认 yes**：直接回车（空串）或以 `y` 开头都算同意。
- 不同意就 `exit`——不是回到菜单，而是终止脚本。

#### 4.3.3 源码精读

空选回退的完整代码在 [selector.sh:51-65](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L51-L65)：

```bash
if [[ ${#selected_packages} -eq 0 ]]; then
    echo $(warning 'You did not select any packages.')
    echo
    echo -n "$(highlight 'Do you want to install default packages?') ($(print_item $@))"
    read -p " $(prompt '[Y/n]') " answer
    case "${answer}" in
        '' | y*)
            selected_packages=("$@")
            ;;
        *)
            echo $(warning 'Installation canceled.')
            exit
            ;;
    esac
fi
```

逐行说明：

- [selector.sh:51](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L51) 用 `${#selected_packages}` 取数组长度，判断是否一个都没选。
- [selector.sh:54](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L54) 的 `$@` 展开原始 target 列表并用青色（`print_item`）显示，告诉用户「默认包」具体指哪些——这很重要，避免用户以为「默认」是个神秘清单。
- [selector.sh:55](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L55) 用 `read -p " 提示 " answer` 把提示符和读取合并成一句；提示里写 `[Y/n]` 表示**大写 Y 在前 = 默认选 Y**。
- [selector.sh:56-59](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L56-L59) 是「同意」分支：空串（直接回车）或任意 `y` 开头（`y`/`yes`/`yeah` 都行）都把 `"$@"` 赋给 `selected_packages`。这里的 `$@` 仍是传给 `select_packages` 的原始参数——回退到「就当没进过菜单」。
- [selector.sh:60-63](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L60-L63) 是「拒绝」分支：任何其他输入都打印取消提示并 `exit`。

一个常被忽略的细节：这段代码**在 `select` 循环之外**。也就是说，只要用户敲了 `.`/`end` 退出循环且啥也没选，就一定会走到这里——包括「一上来就敲 `.`」的情况。它和 4.2 里 `cancel|exit|quit` 的 `exit` 不同：那是「主动取消」，这是「被动确认」，但最终都可能是「什么都不装」。

#### 4.3.4 代码实践

**实践目标**：验证「空选 + 回车默认 yes」会把原始 target 当作结果，且最终安装的就是这些默认包。

**操作步骤**：

1. 仍用上一步的空目录：
   ```sh
   rime_dir=/tmp/rime-select-test bash rime-install --select :preset
   ```
2. 菜单出现后，**直接输入 `.`**（一个也没选）。
3. 观察到黄色的 `You did not select any packages.`，以及高亮的 `Do you want to install default packages? (preset)` 之类提示（括号里显示原始 target）。
4. **直接回车**（保持空回答，命中 `''` 分支）。
5. 观察后续：plum 会像非交互模式一样去安装 `:preset` 里的包。

**需要观察的现象**：

- 提示行括号里出现的是 `:preset`（或你传入的 target），证明「默认包」= 原始 target。
- 直接回车后流程继续，没有再次回到菜单。
- 若第 4 步改为输 `n`，则看到 `Installation canceled.` 并立即退出，不安装任何东西。

**预期结果**：回车默认 yes 时，`/tmp/rime-select-test` 里最终被装入 `:preset` 的 8 个包的源文件（和 `bash rime-install :preset` 结果一致）。

> 待本地验证：提示行里 `$@` 的确切显示形态（是否带颜色、是否换行）依终端而定。

#### 4.3.5 小练习与答案

**练习 1**：用户一进菜单就敲 `cancel`，和「敲 `.` 后在追问里输 `n`」，最终行为有何异同？

**参考答案**：相同点是两者都**不安装任何东西**并以 `exit` 结束。不同点是路径与提示：`cancel` 在 [selector.sh:30-33](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L30-L33) 直接 `exit`，只打印一句 `Installation canceled.`；而「`.` + n」会先经过空选回退（[selector.sh:51-65](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L51-L65)），多打印 `You did not select any packages.` 和默认包确认提示，再在拒绝分支里 `exit`。前者是「主动放弃」，后者是「被动确认后放弃」。

**练习 2**：为什么回退时赋值用的是 `selected_packages=("$@")` 而不是 `selected_packages=("${all_packages[@]}")`？两者内容不一样吗？

**参考答案**：因为语义不同。`all_packages` 是「候选菜单」（已展开、去重后的裸包名），而 `"$@"` 是「调用者最初传入的 target」（可能仍是 `:preset` 这样的集合名）。plum 选择回退到 target 层面，相当于「就当用户从没要求过交互」——保持与 `bash rime-install :preset` 完全一致的行为。若改成 `all_packages`，反而会把展开后的具体包名固化下来，丢失「target」这一层语义。两者在 `:preset` 这种场景下结果集相同，但对 `--select :extra some-package.conf` 这种混合 target 就会有差别。

## 5. 综合实践

把三个模块串起来，设计一个**只读**的代码追踪任务（无需联网，纯阅读理解）：

**任务**：画一张「`rime-install --select :preset` 的一次完整运行」的状态流转图，标注关键变量在每一步的取值。

**步骤**：

1. 从 [rime-install:36](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L36) 开始，记录每一步 `$1`、`interactive`、`targets` 的变化。注意 `shift` 前后 `$@` 的差异。
2. 进入 [selector.sh:6](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L6)，记录 `all_packages` 是如何从 `:preset` 一步步被 `load_package_list_from_target` + 去重填满的（最终应有 8 个元素）。
3. 假设用户依次输入 `1`、`3`、`reset`、`2`、`.`，记录每轮循环后 `selected_packages` 和 `$REPLY` 的值，并标注每轮走的是 `if` 哪个分支或 `case` 哪条。
4. 模拟「`reset` 后又选了 `2`、最后 `.`」，确认 `selected_packages` 最终只剩 `2` 对应的那一个包——这是空选回退**不会**触发的场景（因为非空），直接回到 `rime-install` 主循环安装这一个包。

**预期产出**：一张表或流程图，能解释清楚「`--select` 开关 → 候选构建 → 菜单交互 → 结果回传 → 主循环安装」整条链路上变量的流动。能独立画出这张图，就说明你真正读懂了 selector 模块。

**进阶思考**（可选）：如果要把 `select_packages` 改成支持「多选一次输入」（例如 `1 3 5` 一次加三个），你会改哪个分支、怎么解析 `$REPLY`？提示：`read -a` 可以读入数组，但要注意 `select` 已经先读过一次行了。

## 6. 本讲小结

- `--select` 是**位置敏感的模式开关**：必须作为 `rime-install` 第一个参数，被 `shift` 掉后置 `interactive=1`，本身不会被当作 target（[rime-install:36-39](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L36-L39)）。
- `select_packages` 分两段：先用 `load_package_list_from_target` 把所有 target 展开成候选数组 `all_packages`（用补空格 + `grep -qF` 整词去重），再用 bash 内建 `select` 渲染菜单（[selector.sh:6-49](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L6-L49)）。
- 菜单输入靠 `select` 提供的**双通道**：有效编号赋给 `selected`，原始输入赋给 `REPLY`；编号走 `if`、其余走 `case` 分派。
- 指令语义分四档：`end/ok/0/.` 体面结束、`cancel/exit/quit` 终止脚本、`reset/clear` 清空已选、`[:A-Za-z]*` 手输包名；`case` 的「特例在前、通例在后」保证了保留词不被通配分支误吃。
- 函数靠**全局数组** `selected_packages`（刻意不加 `local`）把结果回传给 `rime-install`，调用必须在同一进程内，结果再替换 `targets` 复用同一条安装主循环。
- 空选回退：循环结束后若一个没选，就追问是否装「原始 target」（默认 yes）；拒绝则 `exit`，保证不会以空列表静默收场（[selector.sh:51-65](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/selector.sh#L51-L65)）。

## 7. 下一步学习建议

- **横向对照 Windows 实现**：本讲讲的是 bash 版 `select_packages` 的交互。plum 在 Windows 侧另有一套批处理安装器（`rime-install.bat`），其交互式安装（双击快捷方式弹出的输入框）走的是完全不同的机制。建议下一讲学 **u3-l2 Windows 批处理安装器**，对比「bash 的 `select` 菜单」与「`.bat` 的 `set /p` 输入」两种交互范式。
- **回看数据来源**：菜单里的候选都来自 `load_package_list_from_target` 对 `*-packages.conf` 的 `source`。如果对 `:preset`/`:extra`/`:all` 的具体内容和清单机制还想加深，可复习 **u2-l7 配置包清单与预设集合**。
- **动手扩展**：如果想给 selector 加「显示包的简介」「按关键字过滤菜单」之类功能，本讲的 `select_packages` 是最直接的修改入口——它已经把候选收集和交互清晰地分成了两段，扩展时只需在对应段插入逻辑。
