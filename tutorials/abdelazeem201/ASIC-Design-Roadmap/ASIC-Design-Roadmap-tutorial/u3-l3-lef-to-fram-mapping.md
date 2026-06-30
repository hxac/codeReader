# LEF 到 Milkyway/FRAM 的层映射

## 1. 本讲目标

本讲解决一个非常具体的问题：**当我们要把厂商提供的 LEF 单元库转成 Synopsys 早期 Milkyway 工具能识别的 FRAM（frame）视图时，为什么需要一个「层号映射文件」，以及 `LEF2FRAM/lef_layer_tf_number_mapper.pl` 这个脚本是如何自动生成它的。**

学完后你应该能够：

1. 说清楚 `.tf`（technology file）里的 `maskName` 和 `layerNumber` 两个字段各自表示什么，以及它们如何构成 Milkyway 的「掩模层编号」。
2. 看懂 LEF 文件里一个层定义块（`LAYER ... TYPE ... END`）的三段式结构，并能手工从一段 LEF 里提取出层名和层类型。
3. 复述脚本对 `MASTERSLICE / CUT / ROUTING / OVERLAP` 四种类型的「派生掩模名」推导规则，尤其是 ROUTING 如何递增出 `metal1 / metal2 / ...`，CUT 如何给出 `via1 / via2 / ...`。
4. 解释 `.map` 映射文件与 `.log` 日志文件的产出格式，知道它们下游会被谁消费。
5. 对比 `.pl`（Perl）与 `.tcl`（Tcl）两个等价实现，体会「同一段文本解析逻辑用两种语言表达」的工程含义。

## 2. 前置知识

本讲承接 [u3-l1 标准单元库与物理数据基础](u3-l1-standard-cell-libraries.md) 与 [u3-l2 创建 NDM 参考库](u3-l2-ndm-library-creation.md)。在继续之前，请确认你已经理解下面几个概念：

- **LEF（Library Exchange Format）**：描述标准单元/宏单元「物理面」的文本文件——单元的外框尺寸、引脚位置、布线阻挡区（OBS）画在哪些层上。它只有「层名」（如 `metal1`），**没有层号**。
- **`.tf`（technology file）**：Synopsys 工艺文件，描述整套金属层栈：每层叫什么名字、是几号掩模（`layerNumber`）、对应的 `maskName`、默认布线方向等。它掌握「层名 → 层号」的权威映射。
- **Milkyway 与 FRAM**：Milkyway 是 Synopsys 较老的库格式（被 ICC，即 IC Compiler 使用）；FRAM 是其中每个单元的「抽象框架视图」，只保留布线需要的几何信息。本仓库 U4 讲的 ICC2 用的是更新的 **NDM** 格式（见 u3-l2），而本讲这套 LEF→FRAM 流程服务于**老的 ICC / Milkyway** 流程（见 [u5-l1 Synopsys ICC 传统流程](u5-l1-icc-legacy-flow.md)）。两者本质都是「把厂商的 LEF 转成工具内部库」，只是目标格式不同。
- **为什么要映射**：Milkyway 在读 LEF 建 FRAM 时，需要知道 LEF 里写的 `metal1` 到底对应 Milkyway 内部数据库里的第几号掩模层。这个「LEF 层名 → Milkyway 层号」的对应，必须从 `.tf` 里查出来，写成一张 `.map` 文件喂给 Milkyway。本讲的脚本就是自动生成这张表的。

> 一个直觉类比：LEF 像「用名字称呼每个人的花名册」，`.tf` 像「把花名册里每个人对应到工号的 HR 表」。脚本的工作就是把这两张表 join（连接）起来，产出一份「名字 + 工号」的最终对照表。

此外你只需会一点**正则表达式**（`^` 行首、`[\s]` 空白、`[\w]` 字符类、`(...)` 捕获组、`+/*` 量词）和**状态机**的思路即可，本讲会边讲源码边复习。

## 3. 本讲源码地图

本讲只涉及 `LEF2FRAM/` 目录下的三个文件，目录非常干净：

| 文件 | 行数 | 作用 |
|------|------|------|
| [LEF2FRAM/Readme.md](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/Readme.md) | ~118 行 | 脚本的使用说明、命令行参数、输出文件命名规则，以及最关键的「LEF2FRAM 两步法」操作流程（先生成 map，再在 Milkyway 里转 FRAM）。 |
| [LEF2FRAM/lef_layer_tf_number_mapper.pl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl) | 199 行 | **Perl 版**主实现。逐行解析 `.tf` 与 LEF，用正则驱动一个状态机，派生掩模名并查表，最后打印 `.map` 与 `.log`。本讲以它为精读对象。 |
| [LEF2FRAM/lef_layer_tf_number_mapper.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl) | 186 行 | **Tcl 版**，逻辑与 Perl 版等价，是把同一套正则状态机移植到 Tcl（`regexp`/`regsub`/`array`）。附带对每行做了一层字符清洗。 |

> 提示：根目录下还有一个同名的 `lef_layer_tf_number_mapper.pl`（见 [u1-l3 仓库目录结构与学习资源地图](u1-l3-repo-structure-map.md) 中提到的「根目录与 LEF2FRAM/ 下重复的 .pl 是同文件副本」），本讲统一引用 `LEF2FRAM/` 下的版本。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开，正好对应脚本从「读 `.tf`」→「读 LEF」→「派生掩模名」→「输出 map/log」的四段执行顺序。

### 4.1 `.tf` 的 `maskName` 与 `layerNumber`

#### 4.1.1 概念说明

一个 `.tf` 工艺文件里，每个会被制造出来的图形层都写成一个 `Layer "名字" { ... }` 块。块里有两个关键字段决定它如何映射到 Milkyway 的掩模层：

- **`layerNumber = N;`**：这一层在掩模编号体系里的**数字编号**，是 Milkyway 数据库内部真正存储的层号。
- **`maskName = "xxx";`**：这一层的**掩模短名**，例如 `metal1`、`via1`、`poly`、`polyCont`。注意它往往是「语义化」的名字，与块外的 `Layer "xxx"` 长名不一定相同。

本脚本的关键设计是：**以 `maskName` 为桥**。最终我们要查的是「`metal1` 是第几号层」，而 `.tf` 里恰好把 `metal1` 这个短名和 `layerNumber` 配对存储，于是脚本先扫一遍 `.tf`，构建一张「`maskName → layerNumber`」的查表，后面扫 LEF 时直接用派生出的掩模名去查这张表。

一个典型的 `.tf` 层块长这样（示例，非仓库内文件，仅示意格式）：

```
Layer "M1" {
    ...
    layerNumber = 20;
    ...
    maskName = "metal1";
    ...
}
```

#### 4.1.2 核心流程

`.tf` 解析采用一个**行驱动的状态机**，三个状态变量：

- `processing_tf_layer`：是否正处于某个 `Layer {}` 块内部。
- `found_maskName`：当前块是否已读到 `maskName`（只有读到 maskName 的块才算「有效掩模层」，才会被存表）。
- `tf_masks`：已收集的有效掩模层数（也用作存表下标）。

伪代码如下：

```
对 .tf 的每一行 line:
    若 line 形如 Layer "名字" {       → 进入一个块，记下 tf_layer_name
    否则若 line 形如 layerNumber = N  → 暂存 tf_layerNumber
    否则若 line 形如 maskName = "x"   → 暂存 tf_maskName，置 found_maskName=1
    否则若 line 是单独的 }（块结束）:
        若 found_maskName==1:
            把 (maskName → layerNumber)、(maskName → tf_layer_name) 存入哈希
            tf_masks++
        退出块状态
```

注意一个细节：`layerNumber` 和 `maskName` 在块内的**出现顺序不固定**，所以脚本只是「暂存」它们，直到遇到块结束 `}` 且确认已拿到 `maskName` 时，才一次性写入哈希表。这是一种典型的「延迟提交」模式。

#### 4.1.3 源码精读

`.tf` 解析用的三个哈希在脚本第 37–39 行声明：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:37-43](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L37-L43) —— 声明三个哈希与状态变量，分别是「下标→maskName」「maskName→layerNumber」「maskName→tf层名」。

进入块、识别字段、结束块的正则与逻辑在主循环里：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:44-85](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L44-L85) —— `.tf` 主解析循环。重点看四条 `if/elsif` 分支对应的四个正则：

- 第 47 行 `^[lL][aA][yY][eE][rR][\s]+\"([\w\W]+)\"[\s]+\{` —— 匹配块头 `Layer "M1" {`，`[\w\W]+` 捕获引号内任意字符作为层名（注意是手写的「大小写不敏感 Layer」写法）。
- 第 56 行 `^[\s]*\}.*$` —— 匹配块尾的 `}`。
- 第 73 行 `layerNumber[\s]*\=[\s]*([\d]+)` —— 抓 `layerNumber = 20;` 里的数字。
- 第 78 行 `maskName[\s]*\=[\s]*\"([\w]+)\"` —— 抓 `maskName = "metal1";` 里引号内的短名。

「延迟提交」发生在第 58–68 行：只有当 `found_maskName == 1`（即本块确实读到了 maskName）时，才把三组关系写入哈希并 `tf_masks++`，随后把 `found_maskName` 复位为 0：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:58-72](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L58-L72) —— 块结束时把 maskName/layerNumber/层名三组映射一次性提交到哈希表。

#### 4.1.4 代码实践

**实践目标**：在不运行脚本的前提下，手工模拟 `.tf` 解析器，验证「延迟提交」的必要性。

**操作步骤**：

1. 假设有如下片段的 `.tf`（示例代码，非仓库文件）：

   ```
   Layer "Via1" {
       maskName = "via1";
       layerNumber = 21;
   }
   ```

   注意这里 `maskName` 出现在 `layerNumber` **之前**。

2. 对照第 44–85 行的循环逻辑，逐行走一遍：先命中第 78 行（记下 `via1`，`found_maskName=1`），再命中第 73 行（记下 `21`），最后命中第 56 行的 `}`，在 `found_maskName==1` 条件下提交。

3. 再构造一个「没有 `maskName`」的块（例如某些非制造层），观察它在第 60 行的 `if ($found_maskName == 1)` 判断里被跳过，不会进表。

**需要观察的现象**：块内字段顺序颠倒也不影响最终结果；缺少 `maskName` 的块被静默忽略。

**预期结果**：无论 `layerNumber` 与 `maskName` 谁先出现，提交后 `tf_layerMaskLayerNumber{"via1"}` 都等于 `21`。这就是「暂存 + 块尾统一提交」带来的鲁棒性。

**待本地验证**：如果你手头有真实的 SMIC13 `.tf`（见 Readme 中提到的 `smic13_hd_8lm_1tm_thick.tf`），可以 `grep -nE 'maskName|layerNumber|^Layer'` 它，数一下共有多少个带 `maskName` 的层，与脚本跑完 `.log` 里 `Completed the processing of ...` 的行数对比，应一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么脚本用 `maskName` 而不是块头的 `Layer "M1"` 长名作为哈希的 key？

**参考答案**：因为下游 LEF 派生出的掩模名（`metal1`/`via1`/`poly` 等）是**短名/掩模名体系**，与 `.tf` 的 `maskName` 字段对齐，而不是与厂商自定的 `Layer "M1"` 长名对齐。用 `maskName` 当 key 才能把两边 join 起来。

**练习 2**：第 47 行正则用 `[\w\W]+` 而不是 `[\w]+` 来捕获层名，区别在哪？

**参考答案**：`[\w]+` 只匹配字母数字下划线；`[\w\W]+` 是「任意字符（含换行）」的经典写法，能容忍层名里出现非常规字符。这增强了 `.tf` 兼容性。

---

### 4.2 LEF 的 `LAYER` / `TYPE` 解析

#### 4.2.1 概念说明

LEF 文件里，工艺层定义同样是块状的，但语法和 `.tf` 不同。一个 LEF 层块的标准三段式：

```
LAYER metal1
  TYPE ROUTING ;
  ...（方向、宽度、间距等，脚本不关心）...
END metal1
```

三个关键字：`LAYER <名字>` 开块、`TYPE <类型> ;` 标注层类型、`END <名字>` 关块。脚本只关心**层名**和**层类型**这两样，其余字段（DIRECTION、WIDTH、SPACING 等）全部跳过。

LEF 的 `TYPE` 取值有限，本脚本处理四种：

| LEF TYPE | 含义 | 脚本如何对待 |
|----------|------|--------------|
| `MASTERSLICE` | 多晶硅等基底图形层（通常是第一个） | 派生为 `poly` |
| `CUT` | 通孔切割层 | 根据「上一个层类型」区分 `polyCont` 或 `viaN` |
| `ROUTING` | 金属布线层 | 派生为 `metalN`（N 递增） |
| `OVERLAP` | 重叠/特殊层 | 跳过，不进表 |

#### 4.2.2 核心流程

LEF 解析同样是行驱动状态机，但比 `.tf` 多一个关键变量 `lef_prev_layerTYPE`（上一个已处理层的类型），因为 CUT 类型的派生名**依赖前一个层是不是 MASTERSLICE**：

```
对 LEF 每一行 line:
    若 line 形如 LAYER 名字        → 进块，记下 lef_layer_name
    否则若 line 形如 TYPE XXX ;     → 记下 lef_layerTYPE
    否则若 line 形如 END 名字（块尾）:
        根据 (lef_layerTYPE, lef_prev_layerTYPE, metal_index, lef_masks) 派生掩模名
        用派生名去 .tf 哈希查 layerNumber
        存入 LEF 哈希，lef_masks++
        lef_prev_layerTYPE = lef_layerTYPE   ← 关键：记住当前类型供下一个块用
        退出块状态
```

为什么 CUT 要看「前一个类型」？因为通孔层在物理上夹在两个金属（或一个 poly 一个金属）之间，光看 CUT 本身无法判断它是「poly 到 metal1 的接触孔」还是「metal1 到 metal2 的过孔」。脚本用相邻层类型来推断：紧跟在 MASTERSLICE 后的 CUT 是 `polyCont`，其余的 CUT 是 `viaN`。

#### 4.2.3 源码精读

LEF 主循环与三条匹配分支：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:105-172](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L105-L172) —— LEF 解析主循环，包含开块（L110）、块尾派生（L119）、抓 TYPE（L166）三条分支。

三条正则分别是：

- 第 110 行 `^LAYER[\s]+([\w]+)[\s]*$` —— 严格匹配 `LAYER metal1`（行末不能有别的字符，所以带注释的行不会误中）。注意 LEF 区分大小写，`LAYER` 必须大写。
- 第 119 行 `^END[\s]+[\w]+[\s]*$` —— 匹配 `END metal1`。
- 第 166 行 `^[\s]*TYPE[\s]+([\w]+)[\s]*\;` —— 匹配 `  TYPE ROUTING ;`，捕获 `ROUTING`。注意分号 `\;` 被显式要求。

变量初始化在第 100–104 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:100-104](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L100-L104) —— LEF 状态变量初始化，其中 `lef_prev_layerTYPE` 初值为空串、`metal_index` 初值为 0，是后续派生逻辑的起点。

「块尾用派生名查 `.tf` 表」发生在第 152–160 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:152-164](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L152-L164) —— 用派生掩模名 `$lef_derivedmaskName` 去 `%tf_layerMaskLayerNumber` 查层号，存入 LEF 哈希；最后第 163 行把当前类型赋给 `lef_prev_layerTYPE`，为下一个块做准备。

#### 4.2.4 代码实践

**实践目标**：用脚本里的三条正则，手工解析一段 LEF，体会「严格行匹配」过滤掉无关字段的效果。

**操作步骤**：

1. 准备一段最小 LEF（示例代码）：

   ```
   LAYER metal1
     TYPE ROUTING ;
     DIRECTION HORIZONTAL ;
     PITCH 0.14 ;
   END metal1
   ```

2. 逐行用脚本的三条正则匹配：
   - `LAYER metal1` → 命中第 110 行，进块。
   - `TYPE ROUTING ;` → 命中第 166 行，`lef_layerTYPE="ROUTING"`。
   - `DIRECTION HORIZONTAL ;` 与 `PITCH 0.14 ;` → **三条正则都不命中**（不满足 `^LAYER`、`^END`、`TYPE` 开头），被忽略。
   - `END metal1` → 命中第 119 行，触发派生。

3. 试着把第二行改成 `MACRO metal1 ...` 或在 `LAYER metal1` 行尾加注释 `LAYER metal1 # comment`，观察是否还能进块。

**需要观察的现象**：第 110 行正则要求行末是 `$`，所以 `LAYER metal1 # 注释` 这种带行尾注释的写法**不会**进块——这正是脚本对 LEF 格式「干净度」的隐含要求。

**预期结果**：只有完全符合三段式、且字段独占一行的 LEF 块才会被解析；任何附带的非标准行都被静默跳过。

**待本地验证**：若你拿到一份真实 LEF，可先用脚本自带的 `print_log`（见 4.4）观察日志里 `Beginning to process ... layer` / `Completed ...` 成对出现的次数，与 LEF 里 `LAYER` 关键字数量比较。

#### 4.2.5 小练习与答案

**练习 1**：第 110 行正则结尾是 `[\s]*$`，如果改成 `.*$`（允许行尾任意内容），会带来什么风险？

**参考答案**：会误把 `LAYER metal1 extra` 或带行内注释的行也当作合法层头，导致层名捕获错误或块状态错乱。严格的 `$` 锚定是对 LEF 格式的防御性约束。

**练习 2**：脚本靠 `lef_prev_layerTYPE` 区分两种 CUT，这依赖于 LEF 里层的**什么性质**？

**参考答案**：依赖于 LEF 中层定义的**物理先后顺序**——通孔层总是紧跟在它连接的金属/多晶层之后出现。脚本隐式假设 LEF 层顺序符合真实物理栈序。

---

### 4.3 派生掩模名规则（本讲核心）

#### 4.3.1 概念说明

这是整个脚本最精巧的部分。LEF 只给层名（如厂商命名 `M1`、`VIA1`）和类型，**不给** Milkyway 那套语义化的掩模短名（`metal1`/`via1`/`poly`）。脚本的任务就是「根据类型和上下文，把每个 LEF 层**派生**成一个标准掩模名」，再用这个名字去 `.tf` 查层号。

派生规则是一组 if/elsif 优先级链，核心是一个自增计数器 `metal_index`。它只在遇到 `ROUTING` 层时递增，用来编号金属层；通孔层则复用当前 `metal_index` 来编号。

#### 4.3.2 核心流程

派生规则用伪代码表示（优先级自上而下，命中即停）：

```
若 TYPE==MASTERSLICE 且 lef_masks==0（即第一个层）→ "poly"
否则若 TYPE==CUT 且 上一类型==MASTERSLICE          → "polyCont"   （poly 接触孔）
否则若 TYPE==ROUTING:
    若 metal_index==0: metal_index = 1             （第一次遇到金属，置 1）
    否则:             metal_index++                （后续金属，自增）
    → "metal" + metal_index
否则若 TYPE==CUT 且 上一类型!=MASTERSLICE            → "via" + metal_index
否则若 TYPE==OVERLAP                                → 跳过（不进表）
否则                                                → die 报错退出
```

`metal_index` 的递增可以写成一段简单数学。设第 \(k\) 个被处理的 ROUTING 层（\(k \ge 1\)），则它派生出的金属下标为：

\[
\text{metal\_index}^{(k)} = k
\]

而它之后紧跟的一个 CUT 通孔层会复用同一个下标，派生为 \(\text{via}k\)，表示「连接 metal\(k\) 与 metal\((k+1)\) 的通孔」。

一次典型的 LEF 层序列会被派生成下表（设物理栈序为 poly→contact→metal1→via1→metal2→via2）：

| LEF 块顺序 | TYPE | 上一类型 | metal_index | 派生掩模名 |
|-----------|------|---------|-------------|-----------|
| 1 | MASTERSLICE | — | 0 | `poly` |
| 2 | CUT | MASTERSLICE | 0 | `polyCont` |
| 3 | ROUTING | CUT | 0→1 | `metal1` |
| 4 | CUT | ROUTING | 1 | `via1` |
| 5 | ROUTING | CUT | 1→2 | `metal2` |
| 6 | CUT | ROUTING | 2 | `via2` |

注意第 3、5 行：第一次 ROUTING 把 `metal_index` 从 0 **置为 1**（不是自增到 1，见下方源码细节），第二次才自增到 2。两者结果相同但代码写法不同，是为了保证「金属从 1 开始编号」。

#### 4.3.3 源码精读

派生规则的完整 if/elsif 链在第 123–150 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:123-150](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L123-L150) —— 派生掩模名的优先级判断链，覆盖 poly / polyCont / metalN / viaN / OVERLAP / 报错六种分支。

本讲「代码实践任务」聚焦的 ROUTING 分支在第 132–137 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:132-137](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L132-L137) —— 当 `lef_layerTYPE eq "ROUTING"` 时：若 `metal_index==0` 置为 1，否则自增，最后拼成 `"metal".$metal_index`。这段就是 `metalN` 推导的全部逻辑。

```perl
elsif ( ($lef_layerTYPE eq "ROUTING") )
{
    if ( $metal_index == 0) { $metal_index = 1; }
    else { $metal_index++; }
    $lef_derivedmaskName = "metal".$metal_index;
}
```

CUT（非 MASTERSLICE 之后）分支在第 138–142 行，复用当前 `metal_index`：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:138-142](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L138-L142) —— 通孔派生为 `"via".$metal_index`，下标沿用最近一个金属层的编号。

两个边界值得注意：

- 第 124 行 `$lef_masks == 0` 的判断：只有**第一个** MASTERSLICE 才派生成 `poly`。如果 LEF 里后续又出现 MASTERSLICE（罕见），它既不满足第 124 行（`lef_masks` 已 >0），又不满足其它分支，会落到第 149 行的 `die "Can not process due to issue..."` 退出。
- 第 149 行的 `die`：任何未被规则覆盖的 TYPE 组合都会让脚本直接报错终止，这是一种「严格模式」——宁可失败也不输出错误的映射。

#### 4.3.4 代码实践（对应本讲指定实践任务）

**实践目标**：精确描述「当 LEF 层 TYPE 为 ROUTING 时如何推导出 metalN 掩模名」，并手工预测一段 LEF 序列的派生结果。

**操作步骤**：

1. 阅读 [第 132–137 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L132-L137)，用一句话写下 ROUTING 的推导规则（见下方「预期结果」）。
2. 给定如下 LEF 片段（示例代码），仅含 TYPE 信息，预测每个块的派生名：

   ```
   LAYER poly       TYPE MASTERSLICE ;  END poly
   LAYER cont       TYPE CUT ;          END cont
   LAYER m1         TYPE ROUTING ;      END m1
   LAYER v1         TYPE CUT ;          END v1
   LAYER m2         TYPE ROUTING ;      END m2
   LAYER v2         TYPE CUT ;          END v2
   ```

3. 画出 `metal_index` 在每个 `END` 时的取值，与 4.3.2 的表格对照。

**需要观察的现象**：`metal_index` 只在 ROUTING 块结束时改变；CUT 块不改 `metal_index` 但借用它命名 via。

**预期结果**：

- ROUTING 推导规则：**`metal_index` 初值为 0；每遇到一个 ROUTING 层，若它是第一个金属（`metal_index` 仍为 0）则置 1，否则自增 1；派生名 = 字符串 `"metal"` 拼上当前 `metal_index`。** 因此第 1 个 ROUTING → `metal1`，第 2 个 → `metal2`，依此类推。
- 上面片段的派生结果依次为：`poly` → `polyCont` → `metal1` → `via1` → `metal2` → `via2`，与 4.3.2 表格完全吻合。

**待本地验证**：若有真实 LEF，可在脚本第 136 行后临时加一行 `print "DEBUG metal_index=$metal_index name=$lef_derivedmaskName\n";`，运行后核对每个金属层的下标是否如预期从 1 递增。（注：这只是建议的调试手段，本仓库源码未包含此行。）

#### 4.3.5 小练习与答案

**练习 1**：如果一份 LEF 里**没有** MASTERSLICE 层，第一个出现的层直接是 `CUT`，脚本会怎样？

**参考答案**：第 124 行要求 `MASTERSLICE && lef_masks==0` 才给 `poly`；第一个 CUT 不满足「上一类型==MASTERSLICE」（此时 `lef_prev_layerTYPE` 是初值空串），所以走到第 138 行派生为 `"via".$metal_index`，即 `via0`（因为还没遇到金属，`metal_index` 仍为 0）。这通常是个**异常信号**——说明 LEF 缺少 poly 层，`via0` 在 `.tf` 里很可能查不到对应层号，下游会报错。

**练习 2**：为什么通孔编号复用「上一个金属」的下标，而不是「下一个金属」？

**参考答案**：因为 LEF 中通孔层紧跟在它**下方**的金属层之后定义（物理栈序），此时 `metal_index` 还停留在下方金属的编号上，复用它得到 `viaN` 表示「metalN 上方的通孔」，与 `metal1 上方是 via1` 的常用命名一致。若用「下一个金属」编号，就需要预读，状态机会复杂得多。

**练习 3**：第 149 行 `die` 在什么情况下会触发？这种「直接退出」的设计有何利弊？

**参考答案**：当 TYPE 是规则未覆盖的组合时触发（例如第二个 MASTERSLICE，或 LEF 出现 `TYPE IMPLANT ;` 这类注入层）。好处是**绝不输出错误映射**，避免 silently 把错的层号喂给 Milkyway；代价是对非标准 LEF 兼容性差，需要人工预处理。第 5–6 行注释「Updated to cover more LEF cases」正是作者逐步扩展规则、减少 `die` 命中率的过程。

---

### 4.4 映射文件与日志输出

#### 4.4.1 概念说明

脚本最后产出两个文件，命名规则在参数处理阶段就定好了：

- **`.map` 文件**：最终交付物。每行一个层，格式为 `<LEF层名> <层号>`，供 Milkyway 在做 LEF→FRAM 转换时读取。
- **`.log` 文件**：处理过程的详细日志，记录每层的发现/完成、派生过程、跳过的 OVERLAP 层等，便于排查「为什么某个层没进表」或「为什么层号对不上」。

文件名由输入文件名拼成：把 `.tf` 后缀换成 `_tf`、`.lef` 换成 `_lef`，再拼成 `<lef>_lef_<tech>_tf.map` 与 `.log`。例如 `scc013u.lef` + `smic13.tf` → `scc013u_lef_smic13_tf.map`。

#### 4.4.2 核心流程

输出分两步：

1. **参数阶段**就生成输出文件名并打开句柄（写模式）。
2. **主流程结束**后，一个 `for` 循环遍历所有 LEF 层，对每一层调用 `print_map` 打印「层名 + 空格 + 层号 + 换行」。

日志则在整条流水线里**边做边记**——每个正则命中、每个块的开始结束，都通过 `print_log` 同时输出到屏幕和 `.log` 文件。

#### 4.4.3 源码精读

输出文件名拼接在第 21–26 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:21-27](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L21-L27) —— 用 `s/\.tf/_tf/`、`s/\.lef/_lef/` 改后缀，再拼接出 `.map` 与 `.log` 文件名。注意 `$tmp_s1`、`$tmp_s2` 的顺序：`.lef` 在前、`.tf` 在后。

打印 `.map` 的循环在第 182–186 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:182-186](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L182-L186) —— 遍历 `0..lef_masks-1`，每行先打 LEF 层名、再打一个空格和层号。两层 `print_map` 之间没有换行，靠第二个 `print_map` 末尾的 `\n` 收尾，因此每层恰好占一行。

两个辅助子例程在第 190–198 行：

[LEF2FRAM/lef_layer_tf_number_mapper.pl:190-198](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.pl#L190-L198) —— `print_log` 同时写屏幕与 `.log`；`print_map` 同时写屏幕与 `.map`。把「同时输出到两处」封装成函数，避免每次都写两遍。

`.map` 文件最终长这样（示例，非仓库内文件）：

```
poly 10
polyCont 15
metal1 20
via1 21
metal2 22
via2 23
```

每行左列是 LEF 里的层名，右列是从 `.tf` 查出的掩模层号。Readme 里第 31–32 行也确认了这两个输出文件名：

[LEF2FRAM/Readme.md:29-32](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/Readme.md#L29-L32) —— 文档列出的两个输出文件：`*.map`（LEF 层到工艺层层号映射）与 `*.log`（处理日志）。

#### 4.4.4 代码实践

**实践目标**：完整跑通一次脚本（或在没有 EDA 环境时做一次「纸上运行」），观察两个输出文件的内容与命名。

**操作步骤**（参照 [Readme.md 的 LEF2FRAM 两步法](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/Readme.md#L65-L94)）：

1. （可选环境准备）Readme 第 69–78 行提醒：先把脚本第 1 行 shebang 从 `/depot/perl-5.003/bin/perl5.003` 改成系统实际的 `#!/usr/bin/perl -w`，并 `chmod 755`。
2. 准备同目录下的 `.tf` 与工艺 LEF（Readme 第 81–86 行强调要用**工艺 LEF**，即 process LEF，而不是 SRAM 生成的 LEF）。
3. 执行命令：

   ```bash
   ./lef_layer_tf_number_mapper.pl smic13_hd_8lm_1tm_thick.tf scc013u_8lm_1tm_thick.lef
   ```

   （命令来自 [Readme.md 第 91 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/Readme.md#L91-L91)。）

4. 打开生成的 `scc013u_8lm_1tm_thick_lef_smic13_hd_8lm_1tm_thick_tf.map` 与同名 `.log`。

**需要观察的现象**：`.map` 每行「层名 层号」对齐；`.log` 里能看到每个层的 `Beginning to process ...` / `Found the TYPE ...` / `Completed ...` 三段式记录，以及 OVERLAP 层的 `Skipping ...`。

**预期结果**：`.map` 行数 = LEF 中有效层数（MASTERSLICE+CUT+ROUTING，不含 OVERLAP）；`.log` 的 `Completed` 行数与 `.map` 行数一致。

**待本地验证**：本仓库**不附带** `.tf` 与 LEF 样例输入（`LEF2FRAM/` 下只有脚本与 Readme），且脚本依赖具体厂商库，因此无法在沙箱内真实运行。如需验证，需自备 SMIC13 或类似工艺库文件；或按 4.1.4 / 4.3.4 的「纸上运行」方式手工对照。Readme 第 86 行也提示：同一工艺族下不同单元库的 `.tf` 与工艺 LEF 通常相同（仅 RDL 变体有差异），可任选一份。

#### 4.4.5 小练习与答案

**练习 1**：`.map` 第 185 行第二个 `print_map` 末尾带 `\n`，而第一个（第 184 行）不带。如果把这两个 `\n` 位置或有无改错，会出现什么？

**参考答案**：第一个 `print_map` 不带换行、第二个带，是为了让「层名」和「层号」拼在同一行。若第一个也加 `\n`，则每个层会占两行（层名一行、层号一行），破坏 Milkyway 期望的「每层一行」格式；若第二个去掉 `\n`，则所有层会挤成一行。

**练习 2**：为什么 `print_log` 和 `print_map` 都要把内容同时写到屏幕和文件？

**参考答案**：屏幕输出便于运行时实时观察进度与报错；文件输出留下持久记录（`.log` 供事后排查、`.map` 供下游 Milkyway 读取）。封装成子例程是为了保证「两处一定同步」，避免漏写。

**练习 3**：Readme 第 81 行强调「LEF 应是工艺 LEF，而非 SRAM 生成的 LEF」，为什么？

**参考答案**：工艺 LEF（process/TECH LEF）包含完整的层定义（`LAYER ... TYPE ...`），正是本脚本解析的对象；而 SRAM 厂商生成的宏单元 LEF 通常只含 `MACRO` 块、不含 `LAYER` 层定义，无法用本脚本派生掩模名。本脚本为「工艺 LEF」服务，宏单元 LEF 的转换发生在下游 Milkyway 步骤（Readme 第 97–118 行的 FRAM 生成阶段）。

---

### 4.5（补充）Perl 版与 Tcl 版的对照

`.pl` 与 `.tcl` 是同一算法的两种语言实现。Tcl 版在结构上几乎逐段对应，但有几处值得注意的差异，对二次开发很重要：

1. **每行先做字符清洗**。Tcl 版在进入正则前，对每行做了一串 `regsub` 去掉 `[] {} " ; ()` 等字符（见 [lef_layer_tf_number_mapper.tcl:68-76](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L68-L76) 与 [第 115–122 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L115-L122)）。这是为了在 Tcl 的正则里少处理这些分隔符。代价是这串 `regsub` 中混入了几条可疑的重复/多余模式（如多次替换 `{}`、以及 `[()]}}` 这类括号写法），属于移植时遗留的粗糙点，阅读时需留意。
2. **派生逻辑等价**。Tcl 版第 132–147 行的 if/elseif 链与 Perl 版第 123–150 行一一对应，规则完全相同。
3. **存在冗余赋值**。Tcl 版第 152–153 行连续两次 `set lef_layerDerivedMaskNames($lef_masks) ...`（[见此处](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L152-L156)），是明显的复制粘贴残留，不影响结果但属代码瑕疵。
4. **shebang 不同**。`.pl` 用 `/depot/perl-5.003/bin/perl5.003 -w`（作者机器的旧路径，Readme 提醒要改），`.tcl` 用 `#!/usr/bin/env wish`（Tk 解释器）。

> 结论：学逻辑请读 `.pl` 版（更干净）；若你的环境只有 Tcl（例如要嵌进某 EDA 工具的 Tcl shell），再参考 `.tcl` 版，但使用前建议先核对它的清洗与冗余行是否影响你的输入。

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端纸上工程」：

**任务**：假设你要为一个假想工艺 `mytech.tf` + `mylib.lef` 生成 Milkyway 层映射，请完成以下全部步骤。

1. **造输入**（示例代码，非仓库文件）。写出 `mytech.tf` 的三个层块，要求 `metal1` 的 `layerNumber=20`、`via1=21`、`metal2=22`，且 `maskName` 分别为 `metal1`/`via1`/`metal2`：

   ```
   Layer "M1" { layerNumber = 20; maskName = "metal1"; }
   Layer "V1" { layerNumber = 21; maskName = "via1"; }
   Layer "M2" { layerNumber = 22; maskName = "metal2"; }
   ```

   再写出对应的 `mylib.lef`（注意 LEF 层顺序应反映物理栈序，且需先有 MASTERSLICE 才能派生出 `poly`；为简化，这里从 metal1 开始）：

   ```
   LAYER metal1
     TYPE ROUTING ;
   END metal1
   LAYER via1
     TYPE CUT ;
   END via1
   LAYER metal2
     TYPE ROUTING ;
   END metal2
   ```

2. **预测派生**：用 4.3 的规则，预测三个 LEF 层的派生名与查到的层号。
   - 参考答案：第一个 ROUTING → `metal1`（查得 20）；紧随其后的 CUT（上一类型 ROUTING≠MASTERSLICE）→ `via1`（查得 21）；第二个 ROUTING → `metal2`（查得 22）。

3. **预测输出**：写出 `mylib_lef_mytech_tf.map` 的完整内容：
   ```
   metal1 20
   via1 21
   metal2 22
   ```

4. **找问题**：上述 `mylib.lef` 缺少了 MASTERSLICE（poly）层。请说明：如果把 `metal1` 之前补一个 `LAYER poly / TYPE MASTERSLICE` 块，派生结果会怎样变化？`metal_index` 会不会受影响？
   - 参考答案：补 poly 后，第一个块派生 `poly`（`lef_masks==0` 命中）；若 poly 后还有 CUT，则派生 `polyCont`。但 MASTERSLICE 与 CUT 都**不动 `metal_index`**，所以其后第一个 ROUTING 仍然是 `metal1`，金属编号不受影响——这正是 `metal_index` 只随 ROUTING 递增的设计意图。

5. **（可选，需本地环境）** 真正运行脚本，对比你预测的 `.map` 与实际输出。若无 EDA 库文件，本步骤标注为「待本地验证」。

这个综合任务覆盖了：`.tf` 字段识别（4.1）、LEF 三段式解析（4.2）、派生规则与 `metal_index` 行为（4.3）、以及 map 输出格式（4.4）。

## 6. 本讲小结

- 本讲脚本解决「LEF 层名 → Milkyway 掩模层号」的映射问题，产物是一个 `.map` 文件（外加 `.log`），服务于老的 ICC/Milkyway 流程；ICC2 则改用 NDM（见 u3-l2），两者目标不同但都属「库数据准备」。
- `.tf` 解析用「延迟提交」状态机：在块内暂存 `layerNumber` 与 `maskName`，块尾确认拿到 `maskName` 后才以 `maskName` 为 key 写入哈希，从而容忍字段顺序不固定。
- LEF 解析抓 `LAYER/TYPE/END` 三段，靠严格行末锚定 `$` 过滤掉方向、宽度、间距等无关字段。
- **派生掩模名是核心**：`metal_index` 只随 ROUTING 递增（首个金属置 1，其后自增），CUT 复用当前下标命名 `viaN`，紧跟 MASTERSLICE 的 CUT 为 `polyCont`，首个 MASTERSLICE 为 `poly`，OVERLAP 跳过，其余 `die`。
- `.map` 每行「层名 层号」，命名由输入文件名拼接而成；`print_log`/`print_map` 把内容同时写屏幕与文件。
- `.pl` 与 `.tcl` 是同算法双实现，学逻辑读 `.pl`；`.tcl` 版多了字符清洗层并含若干冗余/可疑行，移植使用前需核对。

## 7. 下一步学习建议

- 顺看 [u5-l1 Synopsys ICC 传统流程](u5-l1-icc-legacy-flow.md)：本讲产出的 `.map` 正是 ICC/Milkyway 流程的输入之一，看完那一讲你会明白这张表下游具体被哪条命令消费、FRAM 视图如何被 ICC 加载。
- 回顾 [u3-l2 创建 NDM 参考库](u3-l2-ndm-library-creation.md) 中 `read_lef` / `read_ndm` 的对比：ICC2 的 NDM 流程把这些映射「内化」进 `create_lib -ref_libs`，不需要手工生成 map 文件——理解两套机制的差异能加深你对「为什么老流程需要这个脚本」的认识。
- 进阶可阅读 [u8-l1 NDR 路由规则自动化](u8-l1-ndr-rule-automation.md)：那里用另一个 Perl 脚本解析同一个 `.tf`（提取 `defaultWidth`/`minSpacing`），与本讲解析 `maskName`/`layerNumber` 形成对照，体会「`.tf` 是一座信息矿，不同脚本各取所需」。
- 若你对 Perl/Tcl 文本处理本身感兴趣，可对比本讲两个脚本的同构分支，作为「把一段解析逻辑从 Perl 移植到 Tcl」的练习素材。
