# NDR 路由规则自动化

## 1. 本讲目标

学完本讲，你应当能够：

- 说清什么是 **NDR（非默认布线规则）**、为什么时钟网络需要它，以及它和 **EM（电迁移）**、**串扰（crosstalk）** 的关系。
- 逐行读懂 [`NDR_rule.pl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl) 这个 Perl 脚本：它如何用正则从 `.tf` 工艺文件里抽出每层金属的 `defaultWidth` 与 `minSpacing`。
- 画出 `found` 计数器的状态机，解释它如何用**同一个整数**同时完成「过滤非金属层」与「判断一层的 width 与 space 是否都收齐」两件事。
- 看懂倍率缩放公式，并准确说出 `perl NDR_rule.pl techfile 2 2` 里两个 `2` 分别作用在谁的身上。
- 把脚本输出的 Tcl 文本通过 `sh` + `eval` 注入 ICC2，用 `create_routing_rule` 自动生成 NDR 规则，替换 [`PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) 里手写硬编码的写法。

## 2. 前置知识

- **NDR 与 CTS（来自 u4-l5）**：CTS（时钟树综合）把理想时钟长成由 buffer 逐级扇出的真实树。时钟网每个周期都在翻转，是整颗芯片中活动最频繁、负载最大的网络，因此需要「非默认布线规则」（NDR, Non-Default Routing Rule）给它**加宽线宽、加大间距**。
- **`.tf` 工艺文件（来自 u3-l1）**：`.tf` 是 Synopsys 的工艺文件，描述金属层栈、每层的默认宽度 `defaultWidth`、最小间距 `minSpacing`、布线方向等。本仓库**不含真实 `.tf`**（在整个仓库里搜不到任何 `.tf` 文件，它通过 `common_setup.tcl` 里的 `$TECH_FILE` 变量引用，需要使用者自行准备），所以本讲用一小段示例片段来演示脚本行为。
- **Perl 基础**：标量变量以 `$` 开头，`<$TECH>` 逐行读取文件句柄，`=~` 是正则绑定运算符，`@ARGV` 是命令行参数数组，`$1` 是上一个正则捕获的第一组。无需精通 Perl，能逐行读懂即可。
- **ICC2 的 Tcl 注入技巧**：ICC2 的 `sh` 命令可以调用外部 shell 程序并把它的标准输出捕获成 Tcl 字符串，`eval` 可以把一段字符串当作 Tcl 代码执行——这是把外部脚本输出「喂」进 ICC2 的关键两步。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`IC Compiler II/NDR_rule.pl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl) | Perl 自动化脚本：解析 `.tf`，按倍率缩放每层金属的宽度/间距，输出 `set WIDTH {...}`、`set SPACE {...}` 两行 Tcl 文本，供 ICC2 直接 `eval`。 |
| [`IC Compiler II/PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | ICC2 物理设计主脚本。第 154–161 行的 CTS 段手写了 `create_routing_rule` + `set_clock_routing_rules`，正是 `NDR_rule.pl` 想要自动化、替代掉的对象。 |

## 4. 核心概念与源码讲解

本讲把 `NDR_rule.pl` 拆成四个最小模块讲：先讲「为什么需要 NDR」（动机），再讲「怎么从 `.tf` 里把数值抠出来」（正则解析），再讲「怎么判断收齐了、怎么缩放」（`found` 状态机 + 倍率），最后讲「怎么把结果用回 ICC2」（输出与 `create_routing_rule`）。

### 4.1 NDR 与时钟网络

#### 4.1.1 概念说明

ICC2 默认让信号网按 `.tf` 里规定的 `defaultWidth`（默认宽度）和 `minSpacing`（最小间距）布线——这是「默认规则」。而 **NDR（Non-Default Routing Rule，非默认布线规则）** 是用户额外定义的一套更宽松的规则：**更宽的线、更大的间距**。

时钟网之所以需要 NDR，是因为它和普通信号网有两个本质不同：

1. **EM（电迁移）风险高**：时钟网每个时钟周期都翻转，平均电流大。金属线长期被大电流冲击，金属原子会被逐渐「冲走」，最终断路或短路。线越宽，电流密度越低（电流密度 \(J = I/A\)，截面积 \(A\) 随线宽增大），EM 寿命越长。
2. **串扰敏感**：时钟网遍布全芯片，与大量信号网平行长距离走线，寄生耦合电容大。相邻信号翻转会在时钟线上耦合出噪声、改变时钟沿到达时间，进而恶化 **skew**。间距越大，耦合电容越小，串扰越小。

因此工程上常给时钟网定义一套「2 倍宽、2 倍间距」之类的 NDR（写作 `2Wx2S`），让时钟树综合（CTS）阶段把时钟网按这套规则布线。

#### 4.1.2 核心流程

```
.tf 工艺文件（每层默认 width/spacing）
        │  乘以倍率（如 width×2, spacing×2）
        ▼
   NDR 规则（每层加宽后的 width/spacing）
        │  create_routing_rule 定义规则
        ▼
   set_clock_routing_rules 把规则绑给时钟网
        │  clock_opt / 路由器据此布时钟网
        ▼
   时钟网走「更宽、间距更大」的金属
```

#### 4.1.3 源码精读

`PnR.tcl` 在 CTS 段手写了这样一段（这是「手动」做法，也是 `NDR_rule.pl` 要替代的对象）：

[IC Compiler II/PnR.tcl:154-161](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L154-L161) —— 手动定义路由规则并绑给时钟网络：

```tcl
create_routing_rule ROUTE_RULES_1 \
 -widths {M3 0.2 M4 0.2 } \
 -spacings {M3 0.42 M4 0.63 }
set_clock_routing_rules -rules CLK_SPACING -min_routing_layer M2 -max_routing_layer M4
```

这里有两个值得注意的点：

- `-widths` / `-spacings` 后面跟的是一个 **扁平的「层 值 层 值」列表**，即 `{M3 0.2 M4 0.2}` 表示「M3 层宽 0.2，M4 层宽 0.2」。`NDR_rule.pl` 输出的正是这种格式。
- 这是一个**模板片段，存在不一致**：`create_routing_rule` 创建的规则叫 `ROUTE_RULES_1`，但下一行 `set_clock_routing_rules -rules` 引用的却是 `CLK_SPACING`——两者对不上。真实项目里必须把它们改成同一个名字才能生效。这种「手写硬编码 + 名字写错」正是 `NDR_rule.pl` 想用自动化消除的痛点。

#### 4.1.4 代码实践

**实践目标**：体会「手写 NDR 有多繁琐、多易错」。

**操作步骤**：

1. 打开 `PnR.tcl` 第 156–160 行。
2. 数一数：这段手写规则只覆盖了 M3、M4 两层，但一颗芯片常有 M1–M9 共 9 层金属。如果要给全部 9 层都写一遍 `widths`/`spacings`，每层还要查 `.tf` 里的默认值再乘倍率，工作量与出错概率有多大？
3. 试着把第 159 行的 `CLK_SPACING` 改成 `ROUTE_RULES_1`，让绑定关系自洽。

**需要观察的现象**：手写方式下，倍率换一次（比如从 `2Wx2S` 改成 `2Wx3S`），9 层的 18 个数字（9 个 width + 9 个 spacing）都要重新手算重填。

**预期结果**：你会直观感受到——这正是用一个脚本「读 `.tf` → 自动算 → 自动生成」的价值所在。

#### 4.1.5 小练习与答案

**练习 1**：时钟网为什么对 EM 特别敏感，而普通数据网相对没那么敏感？

> **答案**：时钟网每个周期都翻转，是「翻转率（activity factor）」最高的网络，平均电流大；而很多数据网只在偶尔变化时才翻转，平均电流小。EM 寿命与电流密度强相关，所以时钟网最需要靠 NDR 加宽来降低电流密度。

**练习 2**：NDR 加大间距主要是为了改善哪一个时序指标？

> **答案**：主要是减小耦合电容、降低串扰引起的噪声和时钟沿抖动，从而帮助控制 **clock skew**（时钟偏斜）。

---

### 4.2 `.tf` 正则解析（提取 defaultWidth / minSpacing）

#### 4.2.1 概念说明

`.tf` 文件里，每一层金属的描述大致长这样（Synopsys 风格）：

```
Layer "M1" {
    routing_direction = vertical;
    defaultWidth = 0.05;
    minSpacing = 0.06;
    ...
}
```

`NDR_rule.pl` 要做的事，就是**逐行扫一遍 `.tf`**，对每一个 `Layer` 块，抓住两件东西：层名（`M1`）和它的 `defaultWidth`、`minSpacing` 两个数。它用的工具就是**正则表达式**——把每一行文本去「匹配」一个模式，匹配上了就把里面的数字抠出来。

#### 4.2.2 核心流程

脚本主循环对每一行做四件事（顺序就是代码里的顺序）：

```
读入一行 line
  │
  ├─ ① 若行里有 "}"  → 复位 found = 0（一个 Layer 块结束了）
  ├─ ② 若行是 "Layer 名" → 记下层名，并按「是不是金属」给 found 一个初值
  ├─ ③ 若行里有 defaultWidth → 抠出数字，若合理就把 found +1
  ├─ ④ 若行里有 minSpacing   → 抠出数字，若合理就把 found +1
  └─ ⑤ 若 found == 2 → 这一层两个值都齐了，缩放并追加输出
```

#### 4.2.3 源码精读

整个解析主循环在 [IC Compiler II/NDR_rule.pl:66-99](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L66-L99)。逐块看：

**识别一个 Layer 块的开始**（[NDR_rule.pl:73-77](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L73-L77)）：

```perl
if ($line =~ /^Layer\s+(\S+)/) {
    $name = $1;
    $name =~ s/[\"\s]//g;
    $found = ($name =~ /^M\d+$/) ? 0 : -1;  # 金属层→0，非金属→-1
}
```

- `^Layer\s+(\S+)`：匹配「行首是 `Layer`，后面跟空白，再跟一段非空白字符」，把这段非空白字符捕获到 `$1`。对 `Layer "M1" {`，`$1` 是 `"M1"`（含引号）。
- `s/[\"\s]//g`：把引号和空白字符全删掉，得到干净的 `M1`。
- 关键一行：`$found = ($name =~ /^M\d+$/) ? 0 : -1`。`^M\d+$` 表示「整个字符串是 `M` 后面跟一个或多个数字」。所以 `M1`、`M9` 匹配（`found=0`），而 `via1`、`poly`、`MRDL` 都不匹配（`found=-1`）。**这一行同时决定了「只处理金属层」**。

**抠出 defaultWidth**（[NDR_rule.pl:80-83](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L80-L83)）：

```perl
if ($line =~ /defaultWidth\s+([\d.]+)/) {
    $width = $1;
    $found++ if ($width > 0 && $width < 1);
}
```

- `[\d.]+` 匹配「由数字和点组成的串」，比如 `0.05`。捕获到 `$width`。
- `($width > 0 && $width < 1)` 是一道**合理性闸门**：只接受小于 1 的值。原因：先进工艺的金属线宽在微米单位下通常是零点几 μm（如 0.05、0.24、0.84）。若某个值 ≥ 1，多半不是真正的金属线宽（或是异常数据），就不计数。

**抠出 minSpacing**（[NDR_rule.pl:86-89](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L86-L89)）：写法和 `defaultWidth` 完全对称，只是正则换成 `minSpacing`，变量换成 `$space`，同样过 `< 1` 闸门。

#### 4.2.4 代码实践

**实践目标**：验证你对三个正则的理解。

**操作步骤**：拿下面这三行，分别判断它们会被 ②③④ 哪一条匹配、`$1` 捕获到什么：

```
Layer "M3" {
    defaultWidth = 0.07 ;
    minSpacing=0.08;
```

**需要观察的现象 / 预期结果**：

| 行 | 命中的分支 | 捕获 `$1` |
|----|-----------|-----------|
| `Layer "M3" {` | ② Layer 头 | `"M3"`（清洗后 `M3`，`found=0`） |
| `    defaultWidth = 0.07 ;` | ③ | `0.07`（`$width=0.07`，`found` +1） |
| `    minSpacing=0.08;` | ④ | `0.08`（注意：`minSpacing` 和 `=` 之间**没有空格**，但 `\s+` 要求至少一个空白——这一行**不会**被匹配！） |

第三行是关键陷阱：正则写的是 `minSpacing\s+([\d.]+)`，要求 `minSpacing` 后紧跟空白再跟数字。如果 `.tf` 写成 `minSpacing=0.08`（等号紧贴），就匹配失败、`found` 不会 +1。这说明脚本的正确性**依赖 `.tf` 的具体书写格式**，是个隐性约束（真实 SAED 工艺文件里通常有空格，故能工作）。

#### 4.2.5 小练习与答案

**练习 1**：为什么脚本要区分金属层（`found=0`）和非金属层（`found=-1`）？

> **答案**：NDR 只关心给信号布线的金属层（M1、M2…）。`.tf` 里还有 via（过孔层）、poly（多晶硅）、cut 层等，它们不是布线层、没有有意义的 `defaultWidth/minSpacing`，必须跳过。

**练习 2**：`([\d.]+`）这个字符类能否正确匹配 `0.05`？能否匹配 `1.2e-3` 这种科学计数法？

> **答案**：能匹配 `0.05`（由数字和点组成）。**不能**匹配 `1.2e-3`——因为 `e` 和 `-` 不在 `[\d.]` 里，`([\d.]+`）只会捕获到 `1.2` 就停下。幸好 `.tf` 的线宽/间距都是普通小数，不用科学计数法。

---

### 4.3 `found` 计数器与倍率缩放

#### 4.3.1 概念说明

`found` 是整个脚本的「大脑」——它是一个整数计数器，却身兼二职：

1. **过滤非金属层**：非金属层被初始化成 `-1`，金属层初始化成 `0`。
2. **判断「一层是否收齐 width 和 space」**：每收齐一个有效值就 `+1`，当 `found == 2` 时表示「这一层两个值都齐了」。

这两件事能合并成一个整数，靠的是一个**精巧的「错位」**：非金属层从 `-1` 出发，最多只能涨到 `1`（两个值各 +1），**永远到不了 2**；金属层从 `0` 出发，收齐两个值正好到 `2`，恰好触发。于是同一个 `== 2` 判断，一箭双雕。

#### 4.3.2 核心流程

`found` 的状态机如下：

```
进入一个新 Layer 块
        │
        ├── 层名 ^M\d+$（金属）   → found = 0
        └── 否则（非金属/via/poly）→ found = -1
                 │
        读到 defaultWidth 且 0<w<1 → found++   （0→1 或 -1→0）
                 │
        读到 minSpacing  且 0<s<1 → found++   （1→2 或 0→1）
                 │
        ┌── found == 2 → 触发：缩放 + 追加 W/S + found 归 0
        └── found != 2 → 不触发（非金属层顶多到 1，永远不会触发）
                 │
        读到行末 "}" → found = 0（复位，防止残留污染下一层）
```

> **为什么需要 `}` 复位？** 假设某一层只抠到了 `defaultWidth`、`minSpacing` 缺失或 ≥1，那么这层的 `found` 会停在 `1`。如果没有 `}` 把它复位成 `0`，这个 `1` 会「漏」到下一个 Layer，下一个 Layer 只要再抠到一个值就会变成 `2` 而**误触发**——把 A 层的 width 和 B 层的 space 错配在一起。所以第 70 行的 `$found = 0 if $line =~ /\}/;` 是一道**层间隔离**。

#### 4.3.3 源码精读

**复位**（[NDR_rule.pl:70](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L70)）：`$found = 0 if $line =~ /\}/;`——任何含 `}` 的行都把 `found` 清零。

**触发与缩放**（[NDR_rule.pl:92-98](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L92-L98)）：

```perl
if ($found == 2) {
    my $scaled_width  = $width * $WM;
    my $scaled_space  = $space * $SM;
    $W = "$name $scaled_width $W";
    $S = "$name $scaled_space $S";
    $found = 0;
}
```

这里有两点要看明白：

**第一，倍率缩放**。注意命令行参数的读取顺序（[NDR_rule.pl:55](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L55)）：

```perl
my ($techfile, $SM, $WM) = @ARGV;   # 第2个参数=间距倍率SM，第3个=线宽倍率WM
```

即 `perl NDR_rule.pl techfile 间距倍率 线宽倍率`。缩放公式为：

\[
w_{\text{NDR}}^{(L)} = w_{\text{default}}^{(L)} \times W_M, \qquad s_{\text{NDR}}^{(L)} = s_{\text{default}}^{(L)} \times S_M
\]

所以 `perl NDR_rule.pl saed32nm.tf 2 2` 里，第一个 `2` 是间距倍率（`$SM=2`），第二个 `2` 是线宽倍率（`$WM=2`），结果就是常说的 `2Wx2S`。若想要「线宽不变、间距加倍」的 `1Wx2S`，应写 `perl NDR_rule.pl saed32nm.tf 2 1`。

**第二，前插（prepend）拼串**。`$W = "$name $scaled_width $W";` 把新层拼在字符串**最前面**。由于 `.tf` 里 `M1` 在前、`M9` 在后，先处理 `M1`、后处理 `M9`，前插的结果就是 `M9` 在最前、`M1` 在最后——输出顺序是**从高层金属到低层金属**（降序），这与脚本注释里的示例 `set WIDTH {M9 0.84 M8 0.84 ... M1 0.24}` 一致。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：跟踪 `found` 计数器如何判定「一层已收齐 width 与 space 两个值」，亲手把状态机跑一遍。

**操作步骤**：

1. 准备一段最小示例 `.tf`（保存为 `mini.tf`，**这是为演示自造的示例代码，非仓库原有文件**）：

   ```
   Layer "M1" {
       defaultWidth = 0.05;
       minSpacing = 0.06;
   }
   Layer "via1" {
       defaultWidth = 0.04;
       minSpacing = 0.04;
   }
   Layer "M2" {
       defaultWidth = 0.07;
       minSpacing = 0.08;
   }
   ```

2. 命令行运行（如本机装有 Perl）：`perl NDR_rule.pl mini.tf 2 2`（运行结果待本地验证）。
3. **不依赖运行**，按下表手动跟踪每一行执行后 `found` 的值（`WM=2, SM=2`）：

| 处理到的行 | 命中分支 | found 变化 | 此行结束后 found |
|-----------|---------|-----------|----------------|
| `Layer "M1" {` | ②，名字=M1（金属） | 0（初值） | 0 |
| `defaultWidth = 0.05;` | ③，0<0.05<1 | +1 | **1** |
| `minSpacing = 0.06;` | ④，0<0.06<1 | +1 | **2 → 触发！** 缩放：w=0.1,s=0.12，追加 W=`M1 0.1 `，found 归 0 |
| `}` | ① 复位 | 0 | 0 |
| `Layer "via1" {` | ②，名字=via1（**非金属**） | -1（初值） | **-1** |
| `defaultWidth = 0.04;` | ③ | +1 | 0 |
| `minSpacing = 0.04;` | ④ | +1 | **1（不是 2，不触发）** |
| `}` | ① 复位 | 0 | 0 |
| `Layer "M2" {` | ②，名字=M2（金属） | 0 | 0 |
| `defaultWidth = 0.07;` | ③ | +1 | 1 |
| `minSpacing = 0.08;` | ④ | +1 | **2 → 触发！** 缩放：w=0.14,s=0.16，追加 W=`M2 0.14 M1 0.1 `，found 归 0 |
| `}` | ① 复位 | 0 | 0 |

**需要观察的现象**：

- `via1` 这一层虽然也有 `defaultWidth` 和 `minSpacing`，但因为初值是 `-1`，`found` 顶多涨到 `1`，**永远不会触发**——这就是「用初值 -1 过滤非金属层」的效果。
- `M1` 先处理、`M2` 后处理，但前插拼串让 `M2` 排在了输出前面。

**预期结果**：脚本应在标准输出打印（待本地验证）：

```
set WIDTH {M2 0.14 M1 0.1 };
set SPACE {M2 0.16 M1 0.12 };
```

可以看到 `via1` 完全没有出现在输出里——状态机过滤成功。

#### 4.3.5 小练习与答案

**练习 1**：如果把脚本里的 `$found = ($name =~ /^M\d+$/) ? 0 : -1;` 改成 `? 0 : 0`（即非金属层也初始化成 0），会发生什么？

> **答案**：非金属层（如 `via1`）也会从 0 出发，抠到两个值后就涨到 2 而被触发，于是 via、poly 等非布线层会混进 `WIDTH`/`SPACE` 输出，污染结果。这正是初值必须取 `-1` 的原因。

**练习 2**：若某层金属的 `defaultWidth = 1.2`（大于 1），这一层会被怎样处理？

> **答案**：第 83 行的 `($width > 0 && $width < 1)` 闸门不成立，`found` 不 +1。这层会因 `found` 到不了 2 而**被悄悄跳过**，不出现在输出里。这是脚本的隐性限制：对于线宽 ≥ 1μm 的厚顶层金属（某些工艺的顶层或重构层），脚本会漏掉。

**练习 3**：为什么 `}` 复位那一行用 `\}` 而不是 `{`？

> **答案**：因为 `}` 标志一个 `Layer` 块的**结束**。在每个块结束时复位 `found`，可以防止上一层的残留计数（比如只抠到一个值、`found=1`）漏到下一个 Layer，造成跨层错配。`{` 是块开始，开始时的复位由 ② 行的 Layer 头重新赋初值承担。

---

### 4.4 输出 Tcl 变量与 ICC2 `create_routing_rule`

#### 4.4.1 概念说明

Perl 脚本跑在 ICC2 之外，而 `create_routing_rule` 跑在 ICC2 之内。两者怎么对接？答案是：**让 Perl 输出一段合法的 Tcl 文本，再用 ICC2 的 `eval` 把它执行掉**。这是一种非常通用的「脚本胶水」思路——外部工具不直接调用 EDA 命令，而是「写」出 EDA 工具能执行的代码。

#### 4.4.2 核心流程

```
icc2_shell 里：
  set NDR [sh ./NDR_rule.pl $TECH_FILE 2 2]   ← sh 调 Perl，捕获标准输出到 NDR
        │  NDR 的值是字符串："set WIDTH {...};\nset SPACE {...};\n"
        ▼
  eval $NDR                                     ← 把字符串当 Tcl 执行，定义 WIDTH、SPACE
        │  此后 Tcl 里就有了 $WIDTH、$SPACE 两个列表变量
        ▼
  create_routing_rule 2Wx2S -widths $WIDTH -spacings $SPACE
        │  规则定义好
        ▼
  set_clock_routing_rules -rules 2Wx2S -min_routing_layer M2 -max_routing_layer M4
                                                  把规则绑给时钟网
```

#### 4.4.3 源码精读

**脚本头部给出的 ICC2 用法示例**（[NDR_rule.pl:22-28](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L22-L28)）：

```tcl
icc2_shell> set NDR [sh ./NDR_rule.pl TECH_FILE 2 2]
icc2_shell> eval $NDR
icc2_shell> create_routing_rule 2Wx2S -default -widths $WIDTH -spacings $SPACE
```

三步的分工：`sh` 拿到 Perl 的标准输出文本；`eval` 把文本作为 Tcl 代码执行（于是 `WIDTH`、`SPACE` 两个变量被定义进当前 Tcl 作用域）；最后 `create_routing_rule` 消费这两个变量。

> 注：示例里的 `-default` 表示把该规则设为默认规则（出现在脚本注释示例中）。实际给时钟网用 NDR 时，是否加 `-default` 要按项目策略决定——`PnR.tcl` 第 156 行的手写版本就没有 `-default`。本讲以可验证的 `-widths`/`-spacings` 用法为准。

**脚本末尾的输出语句**（[NDR_rule.pl:104-105](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L104-L105)）：

```perl
print "set WIDTH \{$W\};\n";
print "set SPACE \{$S\};\n";
```

`\{` 和 `\}` 在 Perl 双引号字符串里就是字面的 `{` 和 `}`（转义是为了清晰）。于是脚本打印出形如：

```
set WIDTH {M9 0.84 M8 0.84 ... M1 0.24};
set SPACE {M9 0.84 M8 0.84 ... M1 0.24};
```

两行 Tcl 赋值语句。`{...}` 是 Tcl 的「字面列表」，`$WIDTH` 被求值后正好是 `{层 值 层 值 ...}` 这种 `create_routing_rule -widths` 期望的扁平列表。

**对比 `PnR.tcl` 的手写版本**（[PnR.tcl:156-160](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L156-L160)）：手写版只覆盖 M3、M4 两层、数值硬编码，且规则名 `ROUTE_RULES_1` 与绑定名 `CLK_SPACING` 不一致。用 `NDR_rule.pl` 后，可以一行覆盖 M1–M9 全部金属层，倍率换档只需改命令行两个数字，规则名也只出现一次、不会写错。

#### 4.4.4 代码实践

**实践目标**：把 `NDR_rule.pl` 的输出和 `create_routing_rule` 的输入对上号。

**操作步骤**：

1. 假设脚本对某 `.tf` 跑出：
   ```
   set WIDTH {M2 0.14 M1 0.1 };
   set SPACE {M2 0.16 M1 0.12 };
   ```
2. 在脑中执行 `eval` 之后，写出对应的 `create_routing_rule` 命令。

**需要观察的现象 / 预期结果**：

```tcl
create_routing_rule 2Wx2S -widths {M2 0.14 M1 0.1 } -spacings {M2 0.16 M1 0.12 }
```

可以看到 `$WIDTH`、`$SPACE` 替换进去后，正是 `create_routing_rule` 要的「层 值 层 值」列表。再补一行绑定：

```tcl
set_clock_routing_rules -rules 2Wx2S -min_routing_layer M1 -max_routing_layer M2
```

（注意规则名 `2Wx2S` 与 `create_routing_rule` 的第一个参数一致——这就修好了 `PnR.tcl` 手写版里名字对不上的毛病。）

#### 4.4.5 小练习与答案

**练习 1**：为什么必须用 `eval $NDR`，而不是直接 `set WIDTH [sh ./NDR_rule.pl ...]`？

> **答案**：`sh` 的返回值是把 Perl 的标准输出**整段当作一个字符串**。如果直接 `set WIDTH [sh ...]`，`WIDTH` 会变成包含「`set WIDTH {...};\nset SPACE {...};\n`」这整段文字的字符串，而不是数值列表。`eval` 的作用是**把这段文字当作 Tcl 代码执行**，执行后 `WIDTH`、`SPACE` 才作为两个独立列表变量真正被定义。

**练习 2**：脚本输出里 `set WIDTH {M9 ... M1 ...}` 用的是 Tcl 的 `{}` 列表。如果某层缩放后的宽度恰好是 `0.10`，Perl 会打印成 `0.1`（尾零被省略），这会影响 ICC2 吗？

> **答案**：不会。在 Tcl/ICC2 里 `0.1` 和 `0.10` 是同一个数值，`create_routing_rule -widths` 按数值解析，尾零无关紧要。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「从 `.tf` 到 ICC2 NDR 规则」的纸上推演（无需真实 EDA 环境）：

1. **造输入**：写一段含 3 个金属层（M1/M2/M3）和 1 个 via 层的迷你 `.tf`，每层给出 `< 1` 的 `defaultWidth`、`minSpacing`（注意 `minSpacing` 与数字间留空格，避免 4.2.4 里的陷阱）。
2. **跑状态机**：手动跟踪 `found`，确认 3 个金属层都触发、via 层被跳过，写出最终的 `set WIDTH`、`set SPACE` 两行。
3. **换倍率**：把命令改成 `perl NDR_rule.pl mini.tf 3 2`（间距 3 倍、线宽 2 倍），重算每层缩放后的值，体会「只改两个参数，全层自动重算」相比 `PnR.tcl` 手写硬编码的优势。
4. **接 ICC2**：写出完整的 3 行注入代码（`set NDR [sh ...]` → `eval $NDR` → `create_routing_rule ... -widths $WIDTH -spacings $SPACE`），并补一行 `set_clock_routing_rules` 把规则绑给时钟网，确保规则名前后一致。
5. **找茬**：对照 `PnR.tcl` 第 156–160 行，列出用 `NDR_rule.pl` 自动化后能避免的至少两个手写错误（提示：层覆盖不全、规则名不一致、倍率修改要手算）。

> 全程若想真正运行 `perl` 与 ICC2，结果**待本地验证**；本综合实践的目的是让你在不依赖 EDA license 的情况下，也能把脚本的输入、状态机、输出、消费这四段链路在纸上走通。

## 6. 本讲小结

- **NDR** 给时钟网加宽线宽、加大间距，目的是降 **EM**（电流密度 \(J=I/A\)）和降 **串扰**（耦合电容），从而稳住 clock skew。
- `NDR_rule.pl` 用三条正则（`^Layer\s+(\S+)`、`defaultWidth\s+([\d.]+)`、`minSpacing\s+([\d.]+)`）从 `.tf` 抠出每层金属的名字与默认宽度/间距。
- **`found` 计数器**身兼二职：初值 `0`/`-1` 区分金属/非金属层，`==2` 判断一层是否收齐 width 与 space；非金属层从 `-1` 出发永远到不了 2，从而被自动过滤。
- **倍率缩放**：`perl NDR_rule.pl tf SM WM`，第 2 个参数是间距倍率、第 3 个是线宽倍率，\(w_{\text{NDR}}=w_{\text{def}}\times W_M\)、\(s_{\text{NDR}}=s_{\text{def}}\times S_M\)。
- **注入闭环**：`sh` 捕获 Perl 输出 → `eval` 把文本当 Tcl 执行定义 `WIDTH/SPACE` → `create_routing_rule -widths $WIDTH -spacings $SPACE` 自动生成全层 NDR，替代 `PnR.tcl` 里手写硬编码、名字对不上的写法。
- 脚本有两个隐性约束：依赖 `.tf` 里 `minSpacing`/`defaultWidth` 与数字间有空格；默认线宽/间距必须 `< 1` 才会被采纳（厚顶层金属可能被漏掉）。

## 7. 下一步学习建议

- **横向扩展自动化**：下一篇 **u8-l2 Tcl 流程自动化模式** 会把仓库里的 Tcl 自动化技巧（`Vpad.tcl` 的几何坐标循环、`createpathgroup.tcl` 的路径分组、库数据脚本化）汇总，与本讲的「Perl 生成 Tcl」思路互补，建议接着读。
- **回看 CTS 全貌**：本讲聚焦 NDR 自动化，若想补全「NDR 规则如何嵌进整个 CTS 流程」（`set_clock_tree_options`、`clock_opt`、CRPR），请复习 **u4-l5 时钟树综合 CTS**。
- **进阶练习**：尝试改造 `NDR_rule.pl`，让它也能输出「只加倍间距、线宽不变」的 `1Wx2S` 规则，并思考如何把 `minSpacing` 正则放宽为 `minSpacing\s*=?\s*([\d.]+)` 以兼容等号紧贴的 `.tf` 写法。
