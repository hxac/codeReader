# Cadence SKILL 版图脚本入门

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 SKILL 是什么语言、它与 Cadence Virtuoso 版图数据库（cellview）的关系；
- 看懂 `Logo.pl` 里 `let` / `procedure` / `prog` / `for` / `if` 等 SKILL 基本语法；
- 解释单色 BMP 文件头里「像素数据偏移、宽、高、每像素位数、图像数据大小」分别在哪个字节、如何用小端序拼成整数；
- 推导出脚本如何把图像里的一个像素映射成版图层上一个 `Grid × Grid` 的矩形，并手算出它在版图中的 `(x, y)` 坐标；
- 理解 `dbCreateRect` 如何与版图数据库交互，以及这种「图片转版图」脚本在芯片收尾阶段的实际用途。

本讲依赖 u4-l7（收尾与输出）：在那里你已经知道，布线之后还有 filler 插入、金属填充、LVS 等收尾工序。本讲要讲的「在版图顶层金属上画一个 logo」，正是一种常见的、可选的收尾定制——它不影响电路功能，但会出现在最终的 GDSII 里。

## 2. 前置知识

本讲用到的几个概念，先用大白话解释一遍。

- **SKILL 语言**：Cadence（铿腾）公司 EDA 工具（如 Virtuoso 版图编辑器）内置的脚本语言。它像 Lisp，用大量括号、前缀写法（`函数(参数)`），同时允许中缀写法（`a + b`、`a << 2`）。我们用 SKILL 写一段脚本，Virtuoso 就会按脚本去操作版图数据库。
- **cellview（单元视图，cv）**：版图数据库里「一个设计单元」的对象。可以把它理解成「当前这块版图的画布」。脚本里几乎所有几何操作（画矩形、画线）都发生在某个 `cv` 上。
- **Layer（层）**：版图是分层的，比如 `M3`（第三层金属）、`M1`（第一层金属）、`poly`（多晶硅）。本讲脚本把图像画在 `M3` 层上。层通常写成 `list("M3" "drawing")`，即「层名 + 用途」。
- **BMP 位图**：Windows 下一类最简单的图像格式。文件 = 文件头 + 像素数据。本讲只处理 **1 位（mono，单色）BMP**：每个像素只用 1 个 bit，0 或 1，所以 1 个字节能塞下 8 个像素。
- **小端序（little endian）**：多字节整数里，「最低字节」存在「最低地址」。比如 4 字节整数存在地址 `0x0a..0x0d`，那么地址 `0x0a` 处那个字节是最低位、`0x0d` 处那个字节是最高位。
- **位运算**：`<< n` 是左移 n 位（相当于乘 \(2^n\)），`>> n` 是右移 n 位（相当于整除 \(2^n\)）。`bitfield1(数值, 位号)` 取出该数值指定那一位（0 或 1）。
- **坐标与单位**：版图坐标用微米（μm）。本讲里每个像素被画成一个边长 `Grid`（默认 0.3μm）的小方块。

## 3. 本讲源码地图

本讲只涉及一个文件：

| 文件 | 作用 |
|------|------|
| [Logo.pl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl) | 一段 SKILL 脚本（尽管扩展名是 `.pl`，它不是 Perl，而是 SKILL）。读取一张单色 BMP，把每个「黑像素」在当前 cellview 的 `M3` 层上画成一个 `Grid×Grid` 矩形，从而在版图上「印」出一幅图。 |

> 提醒：文件名虽然叫 `Logo.pl`、扩展名是 `.pl`，但它的内容是 **SKILL** 而非 Perl。U3-l3、U8-l1 里的 `.pl` 才是真 Perl。不要被扩展名误导。

## 4. 核心概念与源码讲解

按最小模块拆成 5 小节：SKILL 语言基础 → BMP 文件头解析 → 按位读取像素 → `dbCreateRect` 画矩形 → 版图定制用途。

---

### 4.1 SKILL 语言基础

#### 4.1.1 概念说明

SKILL 是 Cadence EDA 工具的内置脚本语言，运行在 Virtuoso 等工具的解释器里。它的核心特点：

- **括号与函数调用**：和 Lisp 一样，函数调用写成 `函数(参数1 参数2 ...)`，整个表达式用括号包起来。
- **中缀也允许**：算术、比较、位运算可以写成中缀，如 `WORD[0x0d] << 24`。
- **变量要先声明再使用**：顶层用一个 `let` 把所有局部变量名字列出来。
- **多种过程式结构**：`procedure` 定义函数、`prog` 写带 `return`/跳转的过程块、`for` 循环、`if/then/else` 分支。

理解这些，就能把 `Logo.pl` 当成一段「带 C 风格语法的 Lisp」来读。

#### 4.1.2 核心流程

`Logo.pl` 的整体骨架是：

```text
1. let 声明所有局部变量
2. 取当前窗口 win 和当前 cellview cv
3. 配置：BMP 路径、输出层、像素边长 Grid
4. 定义一个弹框辅助函数 MessageForm
5. 把整个 BMP 文件按字节读进数组 WORD[]
6. 从 WORD[] 里按 BMP 规则解析文件头（偏移/宽/高/位数/数据大小）
7. 校验：是不是 BMP？是不是单色？
8. 主循环：逐字节、逐位扫描像素，黑像素 → 画矩形
9. 缩放窗口显示结果，打印结束时间
```

#### 4.1.3 源码精读

最外层用 `let` 把所有用到的变量名一次性声明（SKILL 要求先声明）。窗口与 cellview 在这里取出，配置项也在这里写死：

[Logo.pl:L5-L13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L5-L13) —— 这一段做了三件事：`let` 列出全部局部变量；`hiGetCurrentWindow()`/`getEditRep(win)` 取到当前版图窗口和它的 cellview；`bmpfile`/`Layer`/`Grid` 是三个硬编码配置（输入 BMP 路径、输出层 `M3 drawing`、每个像素边长 0.3μm）。

```skill
let((win cv bmpfile bmpSize WORD Wnum number Grid Layer ... )
    win = hiGetCurrentWindow()
    cv  = getEditRep(win)
    bmpfile = "/home/.../907.bmp"   ;;; 输入 BMP
    Layer   = list("M3" "drawing")  ;;; 输出层
    Grid    = 0.3                   ;;; 每个像素的边长(μm)
```

脚本里还内嵌定义了一个弹框函数 `MessageForm`，遇到错误时弹窗提示：

[Logo.pl:L16-L25](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L16-L25) —— 用 `procedure(名字(参数) 体)` 定义函数；`prog(() ...)` 是过程块（空括号表示没有额外局部变量）；`hiDisplayAppDBox(...)` 是 Virtuoso 弹出对话框的 API。

注意 SKILL 的几个语法细节：

- `;;;` 是行注释（分号开头到行尾都是注释）。
- `;Read BMP file`、`;check bmp file` 等单分号也是注释。
- 文件开头的 `/* ... */` 是块注释（[Logo.pl:L1-L3](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L1-L3) 里是空的）。
- `procedure` 可以嵌套写在 `let` 体内，定义出来的函数之后直接用 `MessageForm(...)` 调用。

#### 4.1.4 代码实践

> 这是一段**源码阅读型实践**（SKILL 解释器随 Cadence Virtuoso 商业工具发行，本机通常无法直接运行）。

1. **目标**：确认你能分清脚本里的「声明 / 赋值 / 调用 / 注释」四类语句。
2. **步骤**：打开 [Logo.pl:L5-L36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L5-L36)，逐行标注每一条语句属于哪一类。
3. **观察**：注意 `hiGetCurrentWindow()`、`getEditRep()`、`hiDisplayAppDBox()` 这些以 `hi` / `ge` / `db` 开头的函数，它们都是 Virtuoso 内置 API——`hi*` 多与窗口/界面有关，`ge*` 与图形选择有关，`db*` 与版图数据库（database）对象有关。
4. **预期结果**：你能列出一个「函数名 → 大致用途」的小表，例如 `hiGetCurrentWindow`：取当前窗口。

#### 4.1.5 小练习与答案

**练习 1**：脚本里同时出现了 `let((...))`、`procedure(...)`、`prog(...)`，它们各管什么？

**答案**：`let` 用来声明并绑定一组局部变量、给出函数体作用域；`procedure` 用来**定义一个可被名字调用的函数**（这里是 `MessageForm`）；`prog` 是带 `return`/跳转能力的过程块，常用于 `procedure` 函数体内部，把一串命令按顺序执行。

**练习 2**：`;;;` 和 `;` 有何区别？

**答案**：在本脚本里没有功能区别——两者都是从分号到行尾的注释。作者用三个分号只是习惯上的强调，方便区分配置项（如 `;;; Input BMP File`）和普通说明。

---

### 4.2 BMP 文件头解析

#### 4.2.1 概念说明

要「把图片画到版图上」，第一步是搞清楚 BMP 文件里像素放在哪、图像多大。BMP 文件由两大部分组成：

- **文件头**：描述「像素数据从第几个字节开始（offset）」「图像宽多少、高多少（像素）」「每个像素用几位」「像素数据区一共多大」等元信息。
- **像素数据**：紧跟在文件头之后，真正存像素的字节流。

对单色（1 位）BMP，1 个字节 = 8 个像素，每行末尾还会补齐到 4 字节的整数倍（行填充，row padding）。脚本不依赖图像处理库，而是**手工按字节读取整个文件**，再依据 BMP 规范从固定字节偏移处取出头信息。

#### 4.2.2 核心流程

1. 用 `infile` 打开文件，`fileLength` 取得文件总字节数。
2. 用 `declare(WORD[大小])` 声明一个字节数组，`for` 循环 + `getc`（读一个字符/字节）+ `charToInt`（转成整数）把整文件逐字节灌进 `WORD[]`。
3. 从 `WORD[]` 的固定偏移处拼出 6 个关键字段。

BMP 头里关键字段的字节位置如下（都是**小端序**多字节整数）：

| 字段 | 字节偏移 | 含义 | 脚本里的变量 |
|------|----------|------|--------------|
| 签名 | 0x00–0x01 | `'B','M'`（0x42,0x4d） | `signature` |
| 像素数据偏移 | 0x0a–0x0d | 像素数据从第几字节开始 | `offset` |
| 宽 | 0x12–0x15 | 图像宽度（像素） | `width` |
| 高 | 0x16–0x19 | 图像高度（像素） | `height` |
| 每像素位数 | 0x1c–0x1d | 单色为 1 | `pixel` |
| 像素数据大小 | 0x22–0x25 | 像素区字节数 | `ImageSize` |

#### 4.2.3 源码精读

先看「整文件读进数组」这一步：

[Logo.pl:L28-L36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L28-L36) —— `if(InFile = infile(bmpfile) then ... else ...)`：先赋值再判断（打开成功才走 then）。成功时 `bmpSize = fileLength(bmpfile)`，`declare(WORD[bmpSize])` 声明数组，`for(Wnum 0 bmpSize-1 WORD[Wnum] = charToInt(getc(InFile)))` 把每个字节读进 `WORD[]`，最后 `close(InFile)`。文件不存在则弹框并 `return()`。

再看从 `WORD[]` 拼出文件头字段：

[Logo.pl:L38-L43](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L38-L43) —— 这 6 行是本模块的核心。

```skill
sprintf(signature "%02x%02x" WORD[0] WORD[1])     ; "42"+"4d" = "424d"
offset    = (WORD[0x0d]<<24) + (WORD[0x0c]<<16) + (WORD[0x0b]<<8) + WORD[0x0a]
width     = (WORD[0x15]<<24) + (WORD[0x14]<<16) + (WORD[0x13]<<8) + WORD[0x12]
height    = (WORD[0x19]<<24) + (WORD[0x18]<<16) + (WORD[0x17]<<8) + WORD[0x16]
pixel     = (WORD[0x1d]<<8) + WORD[0x1c]
ImageSize = (WORD[0x25]<<24) + (WORD[0x24]<<16) + (WORD[0x23]<<8) + WORD[0x22]
```

每行都在做同一件事——**小端序拼整数**。以 `offset` 为例：4 字节整数存在 `0x0a..0x0d`，最低位字节是 `WORD[0x0a]`，最高位字节是 `WORD[0x0d]`，所以拼成：

\[
\text{offset} = \text{WORD}[\text{0x0a}] + (\text{WORD}[\text{0x0b}] \ll 8) + (\text{WORD}[\text{0x0c}] \ll 16) + (\text{WORD}[\text{0x0d}] \ll 24)
\]

签名那行更直观：`WORD[0]=0x42`（`'B'`）、`WORD[1]=0x4d`（`'M'`），`%02x` 把它们格式化成两位十六进制再拼接，得到字符串 `"424d"`。

随后是两道校验关卡：

[Logo.pl:L52-L61](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L52-L61) —— 第一道：`if(!equal(signature "424d") ...)` 检查它确实是 BMP（签名 `BM`）；第二道：`if(!equal(pixel 0x01) ...)` 检查它是**单色**（每像素 1 位）。任一不满足就弹框报错并 `return()`。这就是为什么脚本标题写「only supports mono bmp files」——彩色 BMP 会让「1 字节 = 8 像素」的前提失效。

> **精度说明**：脚本还把解析到的头信息打印出来供核对，见 [Logo.pl:L45-L49](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L45-L49)（用 `printf` 输出 offset/width/height/ImgSize，均以十六进制显示）。

#### 4.2.4 代码实践

1. **目标**：亲手验证小端序拼接公式。
2. **步骤**：假设某 BMP 的字节 `WORD[0x0a]=0x36`、`WORD[0x0b]=0x00`、`WORD[0x0c]=0x00`、`WORD[0x0d]=0x00`，按 `offset` 公式手算 `offset` 的十进制值。
3. **计算**：
   \[
   \text{offset} = 0\text{x}36 + (0 \ll 8) + (0 \ll 16) + (0 \ll 24) = 0\text{x}36 = 54
   \]
4. **预期结果**：`offset = 54`，即像素数据从文件第 54 字节开始（这是 14 字节文件头 + 40 字节 `BITMAPINFOHEADER` 的典型结果）。
5. **若你手头有真实单色 BMP**：可用任意十六进制编辑器打开它，对照上表读出 offset/width/height/pixel，再与脚本运行时打印的 `printf` 输出比较（**待本地验证**：需要 Virtuoso 环境才能跑脚本看到打印）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pixel` 只读 2 个字节（`0x1c,0x1d`），而 `width` 要读 4 个字节？

**答案**：BMP 规范里「每像素位数」是一个 16 位（2 字节）字段，而「宽」是 32 位（4 字节）字段。脚本按字段的真实宽度读取，所以位数用 `(WORD[0x1d]<<8)+WORD[0x1c]` 两个字节，宽用 4 个字节。

**练习 2**：如果用户喂进来一张 24 位彩色 BMP，脚本会怎样？

**答案**：彩色 BMP 的 `pixel` 字段值是 `0x18`（24），不等于 `0x01`，第 [L58-L61](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L58-L61) 行的 `if(!equal(pixel 0x01) ...)` 条件成立，弹框报错 `*ERROR* only supports mono bmp files` 并 `return()`，不会继续画图。

---

### 4.3 按位读取像素

#### 4.3.1 概念说明

拿到 `offset`（像素数据起点）后，剩下的事就是**把像素区每个字节的 8 个 bit 当成 8 个像素**，逐位判断是 0 还是 1。这里有两个关键概念：

- **行填充**：单色 BMP 每行字节数会被补齐到 4 的倍数。比如宽 10 像素的行，理论只需 2 字节（16 bit 够放 10 bit），但要补到 4 字节。所以「每行实际字节数」≠「宽/8」。
- **像素在字节里的顺序**：单色 BMP 里，一个字节的**最高位（bit 7）是这一组 8 个像素里最左边那个**，最低位（bit 0）是最右边那个。

脚本没有用「宽/8」来算每行字节数，而是聪明地用 `ImageSize / height` 直接拿到「每行实际字节数（含填充）」，避开了填充计算。

#### 4.3.2 核心流程

主循环（脚本注释里叫 `BMP2LAY`）的逻辑：

```text
每行实际字节数 BPR  = ImageSize / height          ; 直接用总数据大小除以行数
每行总像素数(含填充) max_column = BPR × 8          ; 因为单色，1 字节 = 8 像素
最后一个字节下标 number = offset + ImageSize - 1

从字节 offset 扫到 number，对每个字节：
    row = (该字节在像素区内的序号) / BPR           ; 第几行
    对该字节的 bit<7>..bit<0> 共 8 位：
        dot = 该位的值(0 或 1)
        column 是一个递增计数器，记录当前是这一行的第几个像素
        若 dot==0（黑像素）且 column < width（未越界到填充区）：画一个矩形
        column++
    若 column 达到 max_column（一行含填充的像素数扫完）：column 清零，换行
```

关键：**用 `column < width` 跳过填充像素**。一行里，`width` 之后到 `max_column` 之间的位都是填充位，不画。

#### 4.3.3 源码精读

先看两个派生量的计算：

[Logo.pl:L64-L65](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L64-L65) —— `max_column = ImageSize/height<<3`：先整除得到每行字节数，再 `<<3`（即 ×8）得到每行像素数（含填充）。`number = offset+ImageSize-1` 是最后一个要处理的字节下标。

> **运算优先级提醒**：`ImageSize/height<<3` 等价于 `(ImageSize/height)<<3`，先除后移位，与上面解释一致。

再看主循环与逐位扫描：

[Logo.pl:L67-L81](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L67-L81) —— 外层 `for(Wnum offset number ...)` 遍历每个字节；`row = fix((Wnum-offset)/(max_column>>3))` 中，`max_column>>3` 又把含填充的像素数变回「每行字节数」，于是 `row` 就是该字节属于第几行（`fix` 是向零取整）。内层 `for(i 0 7 ...)` 处理一个字节的 8 个位。

逐位取值与画图判断在 [Logo.pl:L71-L78](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L71-L78)：

```skill
for(i 0 7
    dot = bitfield1(WORD[Wnum] 7-i)   ; 依次取 bit<7>、bit<6>...bit<0>
    x = Grid*column
    if(zerop(dot) && column<width then
        geSelectObject(dbCreateRect(cv Layer list(x:y x+Grid:y+Grid)))
    )
    column++
)
```

`bitfield1(WORD[Wnum] 7-i)` 取出该字节的第 `7-i` 位：`i=0` 时取 `bit<7>`（最左像素），`i=7` 时取 `bit<0>`（最右像素），正好匹配 BMP「高位在前」的像素顺序。`zerop(dot)` 为真即 `dot==0`，也就是黑像素，于是调用 `dbCreateRect` 画矩形。`column<width` 保证只在实际宽度内画图，填充位不画。

最后 [Logo.pl:L79-L80](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L79-L80) 是换行逻辑：`if(equal(column max_column) column=0)` 一行像素扫完就清零计数器。

> **批判性阅读（待本地验证）**：注意第 [L80](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L80) 行在循环体内又写了 `Wnum++`。而 SKILL 的 `for(Wnum offset number ...)` 本身每轮会自动把 `Wnum` 加 1。如果两者叠加，`Wnum` 每轮实际增加 2，可能**跳过一半字节**、导致渲染出现横向缺列。这一点受 SKILL 具体解释器行为影响，请你在真实 Virtuoso 环境里运行后核对图像是否完整（**待本地验证**）。把它当作「读模板要带批判眼光」的一个真实例子，与 u8-l2 的精神一致。

#### 4.3.4 代码实践

1. **目标**：搞清楚一个字节如何对应 8 个像素，并理解填充跳过。
2. **步骤**：假设某个字节 `WORD[k] = 0b10110001`（即 `0xb1`），`width = 8`，`column` 从 0 开始。手算内层 `for(i 0 7)` 8 次迭代里，哪几次会画矩形。
3. **计算**：按 `bitfield1(byte, 7-i)`，`i=0..7` 依次取 `bit7..bit0`，得到序列 `1,0,1,1,0,0,0,1`。`zerop(dot)` 为真（画矩形）的是取值为 0 的位，即第 2、5、6、7 个像素（从 1 数起）。
4. **预期结果**：这一字节会画出 4 个矩形，分别落在 `column = 1, 4, 5, 6` 的位置（从 0 数起）。
5. **延伸思考**：若 `width = 5`（小于 8），则 `column<width` 会把第 5、6、7 个像素挡掉，只剩 `column=1,4` 两处画矩形——这正是行填充被正确跳过的体现。

#### 4.3.5 小练习与答案

**练习 1**：脚本为什么用 `ImageSize/height` 算每行字节数，而不是 `width/8`？

**答案**：因为单色 BMP 每行会被**补齐到 4 字节整数倍**，实际每行字节数大于等于 `width/8`。用 `ImageSize/height`（总数据大小 ÷ 行数）能直接得到含填充的真实每行字节数，省去单独算填充的麻烦。

**练习 2**：`bitfield1(WORD[Wnum] 7-i)` 里，为什么参数是 `7-i` 而不是 `i`？

**答案**：BMP 单色像素在一个字节里是「高位 = 左像素」。`i=0` 对应这一组最左的像素，必须取最高位 `bit<7>`，所以位号是 `7-i`。若写成 `i`，图像就会左右镜像。

---

### 4.4 `dbCreateRect` 画矩形

#### 4.4.1 概念说明

扫描出「这里有个黑像素」之后，要把它变成版图上看得见的几何图形。Virtuoso 用 `dbCreateRect` 在某个 cellview、某一层上画一个矩形。它是 `db*` 系列（database）API 的一员，直接向版图数据库写入一个矩形对象。

调用形式是：

```skill
dbCreateRect(cv  Layer  list(x1:y1  x2:y2))
```

其中 `cv` 是目标 cellview，`Layer` 是层（如 `list("M3" "drawing")`），`list(x1:y1 x2:y2)` 是矩形的两个对角点（左下角和右上角）。

#### 4.4.2 核心流程

一个像素 → 一个矩形的映射规则：

- 像素在图像里的列号记为 `column`、行号记为 `row`；
- 每个像素边长为 `Grid`；
- 该像素矩形在版图中的左下角坐标为 \((x, y) = (\text{Grid}\cdot\text{column},\ \text{Grid}\cdot\text{row})\)；
- 右上角坐标为 \((x+\text{Grid},\ y+\text{Grid})\)。

于是矩形跨越范围：

\[
\boxed{\;(\text{Grid}\cdot\text{column},\ \text{Grid}\cdot\text{row}) \;\longrightarrow\; (\text{Grid}\cdot\text{column}+\text{Grid},\ \text{Grid}\cdot\text{row}+\text{Grid})\;}
\]

整张图像在版图上占据的范围：x 方向 \(0 \sim \text{Grid}\cdot\text{width}\)，y 方向 \(0 \sim \text{Grid}\cdot\text{height}\)。本仓库默认 `Grid = 0.3`μm，所以一个像素就是版图上 \(0.3 \times 0.3\ \mu\text{m}^2\) 的小方块。

#### 4.4.3 源码精读

画矩形这一行是整段脚本的「落点」：

[Logo.pl:L75](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L75) —— `geSelectObject(dbCreateRect(cv Layer list(x:y x+Grid:y+Grid)))`：

- `dbCreateRect(cv Layer list(x:y x+Grid:y+Grid))` 在 `cv` 的 `Layer` 上画一个矩形，对角点是 `(x, y)` 和 `(x+Grid, y+Grid)`，返回新建的矩形对象；
- 外层 `geSelectObject(...)`（`ge*` = graphics edit）把这个新对象**选中**，方便你在界面里一眼看到刚画的东西。

坐标 `x`、`y` 就在上一行算好：

[Logo.pl:L69](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L69) 与 [Logo.pl:L73](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L73) —— `y = Grid*row`（第 `row` 行的 y 坐标），`x = Grid*column`（第 `column` 列的 x 坐标）。把 `x`、`y` 代入上面的公式，就得到该像素矩形的精确位置。

全部画完后，脚本把窗口缩放到刚画好的区域，让你看清结果：

[Logo.pl:L83](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L83) —— `hiZoomIn(win list(-10:-10 x+10:y+10))` 用最后一个像素的 `x`、`y` 拼出一个略带留白的显示范围，调用 `hiZoomIn` 缩放窗口。

> **细节提醒**：`Layer` 在 [Logo.pl:L12](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L12) 写死为 `list("M3" "drawing")`。如果你的工艺/显示文件里 M3 层名不同，或你想画到别的层（比如顶层金属用来做 logo 更醒目），需要改这一处。

#### 4.4.4 代码实践

1. **目标**：手算几个像素在版图中的矩形坐标，验证你理解了映射公式。
2. **步骤**：设 `Grid = 0.3`，对像素 `(column, row)` 分别取 `(0,0)`、`(3,2)`、`(10,5)`，写出各自矩形的左下角和右上角坐标。
3. **计算**：
   - `(0,0)`：左下 `(0, 0)`，右上 `(0.3, 0.3)`；
   - `(3,2)`：左下 \((0.3\times3,\ 0.3\times2) = (0.9,\ 0.6)\)，右上 \((1.2,\ 0.9)\)；
   - `(10,5)`：左下 \((0.3\times10,\ 0.3\times5) = (3.0,\ 1.5)\)，右上 \((3.3,\ 1.8)\)。
4. **预期结果**：能看到「列号决定 x、行号决定 y、Grid 是像素到微米的缩放系数」这条规律成立。
5. **若在 Virtuoso 中运行（待本地验证）**：准备一张小尺寸单色 BMP（例如 16×16），修改 [Logo.pl:L11](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L11) 的 `bmpfile` 路径指向它，运行后应看到 M3 层出现一组 0.3μm 见方的小矩形拼成的图案。

#### 4.4.5 小练习与答案

**练习 1**：要把 logo 画大 4 倍（每个像素变成原来的两倍边长），改哪里？

**答案**：把 [Logo.pl:L13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L13) 的 `Grid = 0.3` 改成 `Grid = 0.6`。因为坐标公式里 `x`、`y` 都正比于 `Grid`，矩形边长也等于 `Grid`，`Grid` 翻倍会让整图边长翻倍、面积变 4 倍。

**练习 2**：`dbCreateRect` 返回什么？为什么要用 `geSelectObject` 包一层？

**答案**：`dbCreateRect` 返回新建的数据库矩形对象。`geSelectObject` 把这个对象在图形界面里设为「选中」状态，纯粹是为了方便人工查看（高亮显示）。去掉 `geSelectObject`，矩形照样会被创建，只是不会自动选中。

---

### 4.5 版图定制用途

#### 4.5.1 概念说明

把图片「印」到版图上，听起来像玩具，但在芯片工程里有真实用途：

- **公司/项目 Logo**：很多团队会在芯片顶层金属（如 M3、Top Metal）上放一个 logo，流片后在显微镜下或 GDSII 里能看到，兼具识别与美观。
- **版本号/批次标记**：把版本号文字做成单色 BMP，转成版图层上的图形，便于追溯。
- **对准标记 / 测试图形**：某些调试用的简单图形也可用同样办法批量生成。
- **SKILL 二次开发示范**：本脚本本身就是一个「读取外部数据 → 调 `db*` API 写几何」的最小范例，照着它就能写出更复杂的版图自动生成脚本。

它属于芯片收尾（chip finishing）阶段的可选定制，**不改变电路功能**，只增加版图几何，最终会进入 GDSII。

#### 4.5.2 核心流程

一个典型的「图片转版图」工作流：

```text
1. 在图像工具里把 logo 存成「单色 BMP」（每像素 1 位）
2. 在 SKILL 脚本里配置：BMP 路径、目标层、像素边长 Grid
3. 在 Virtuoso 里打开目标 cellview，运行脚本
4. 脚本逐位扫描，用 dbCreateRect 在目标层画出矩形阵列
5. 检查 DRC（这些矩形不能违反设计规则），必要时调整 Grid 或层
6. 随版图一起导出 GDSII
```

#### 4.5.3 源码精读

整个脚本的输入输出边界很清晰，集中在前 14 行的配置：

[Logo.pl:L11-L13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L11-L13) —— 三个配置项决定了脚本的全部「外部依赖」：`bmpfile`（输入图像，目前是写死的绝对路径 `/home/abdelazeem/.../907.bmp`，复用时必须改成你自己的路径）、`Layer`（输出层 `M3 drawing`）、`Grid`（像素边长 0.3μm）。改这三行，就能复用到任意 logo、任意层、任意尺寸。

要复用这段脚本，你需要准备：

| 需要的东西 | 说明 |
|-----------|------|
| 一张单色 BMP | 用图像工具导出为 1 位（mono）BMP；彩色会被第 [L58-L61](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L58-L61) 行挡掉 |
| 目标 cellview | 在 Virtuoso 里打开要画图的单元，`cv = getEditRep(win)` 会取到它 |
| 正确的层名 | 把 `Layer` 改成你的显示文件里真实存在的层 |
| Cadence Virtuoso | SKILL 解释器随该商业工具发行 |

> **与 U4 主流程的关系**：ICC2（`PnR.tcl`）跑完 `write_gds` 出 GDSII 后，如果想在版图上再加 logo，通常是在 Virtuoso 里读入 GDS、跑这段 SKILL、再导出。它和 u4-l7 的「收尾与输出」是衔接关系——logo 属于 GDSII 交付前最后的版图美化。

#### 4.5.4 代码实践

1. **目标**：把脚本改造成「画在顶层金属、像素更大」的版本，并说出改动理由。
2. **步骤**：
   - 把 [Logo.pl:L12](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L12) 的 `Layer` 改成你工艺里更醒目的顶层金属（如 `"M9"`，**待确认**：以你的工艺层名为准）；
   - 把 [Logo.pl:L13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L13) 的 `Grid` 从 `0.3` 改成 `1.0`，让 logo 更显眼；
   - 把 [Logo.pl:L11](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L11) 的 `bmpfile` 改成你自己的单色 BMP 路径。
3. **需要观察的现象**：运行后矩形数应等于「黑像素数」；`Grid` 变大，整图物理尺寸按比例放大；改层后矩形落在新层上。
4. **预期结果**：在 Virtuoso 里看到 logo 出现在指定层、物理边长是 `Grid × 像素数`（**待本地验证**：本机无 Virtuoso，无法实际运行）。
5. **思考题**：为什么画 logo 一般不放在 `M1`？因为 M1 主要用于标准单元内部连线和电源轨（见 u4-l3、u9-l1），上面乱加图形容易引发 DRC 或影响电源连续性；顶层金属更安全。

#### 4.5.5 小练习与答案

**练习 1**：本脚本画出来的矩形是「黑像素」还是「白像素」？依据是哪一行？

**答案**：画的是**黑像素**（`dot == 0` 的位）。依据是 [Logo.pl:L74](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L74) 的 `if(zerop(dot) && column<width then ...)`：`zerop(dot)` 为真即该位是 0，才进入 `dbCreateRect`。在单色 BMP 默认调色板里 0 通常代表黑。

**练习 2**：举出两类「图片转版图」的真实用途。

**答案**：① 在芯片顶层金属放公司/项目 logo 或版本号，用于识别与追溯；② 作为 SKILL 二次开发的最小范例，演示「读外部文件 + `db*` API 写几何」的模式，可扩展为对准标记、测试图形等自动生成脚本。

---

## 5. 综合实践

**任务**：用一张极小的单色 BMP，手工完整推演 `Logo.pl` 会画出的所有矩形坐标，把「BMP 头解析 → 逐位扫描 → 坐标计算 → `dbCreateRect`」整条链路走通。

设这张 BMP 的头信息为（小端序）：

- `offset = 62`（像素数据从第 62 字节开始）；
- `width = 4`，`height = 2`（一张 4×2 的小图）；
- `pixel = 1`（单色）；
- `ImageSize = 16`（每行 8 字节 × 2 行？—— 注意这要与你的填充设定一致）。

请完成：

1. **算每行字节数与每行像素数**：`BPR = ImageSize/height`；`max_column = BPR × 8`。
2. **确认会画几个矩形**：只有 `column < width = 4` 且位为 0 的像素才画，每行最多 4 个候选像素 × 2 行。
3. **写出矩形坐标公式**：对每个有效黑像素 `(column, row)`，矩形为
   \[
   [(\text{Grid}\cdot\text{column},\ \text{Grid}\cdot\text{row}),\ (\text{Grid}\cdot\text{column}+\text{Grid},\ \text{Grid}\cdot\text{row}+\text{Grid})]
   \]
   取 `Grid = 0.3`。
4. **跟踪 `column` 计数器与行填充**：解释为何即使一行有 8 字节，也只在前 `width` 个像素里画图。
5. **画出示意图**：在方格纸上画出 4×2 的像素网格，标出哪些格子会被 `dbCreateRect`，并写出它们在版图中的 `(x, y)` 范围。

**交付物**：一张「像素网格 → 版图矩形坐标」对照表。这个练习把本讲的 5 个最小模块（SKILL 语法、BMP 头解析、逐位扫描、`dbCreateRect`、用途）串起来——你既在解析数据格式，又在算坐标，又理解了它最终如何写入版图数据库。

> **说明**：因为 SKILL 随 Cadence Virtuoso 商业工具发行，本机无法运行，所以本综合实践以「手工推演」为主。若你有 Virtuoso 环境，可再用一张真实的 4×2 单色 BMP 验证你的推演（**待本地验证**）。

## 6. 本讲小结

- `Logo.pl` 虽以 `.pl` 为扩展名，实为 **SKILL** 脚本，运行在 Cadence Virtuoso 里，通过 `db*` API 直接操作版图数据库。
- 脚本先把整张 BMP **逐字节读进 `WORD[]` 数组**，再按 BMP 规范从固定偏移用**小端序**拼出 `offset/width/height/pixel/ImageSize` 等头字段。
- 它只接受**单色（1 位）BMP**（`pixel == 0x01`），因为后面假设「1 字节 = 8 像素」；用 `ImageSize/height` 巧妙绕开了行填充计算。
- 主循环对每个字节的 `bit<7>..bit<0>` 逐位扫描，`zerop(dot)`（黑像素）且 `column<width`（跳过填充）时调用 `dbCreateRect`。
- 像素到版图的映射公式是：像素 `(column, row)` → 矩形 `[(Grid·column, Grid·row), (Grid·column+Grid, Grid·row+Grid)]`，`Grid` 默认 0.3μm。
- 这类「图片转版图」脚本常用于在顶层金属放 logo/版本号，是芯片收尾阶段的可选版图定制，最终随 GDSII 交付；读它时要带批判眼光（注意第 [L80](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Logo.pl#L80) 行 `Wnum++` 的疑似重复递增，待本地验证）。

## 7. 下一步学习建议

- **U10-l1（逻辑综合与 yosys）**：回到流程主线，看 RTL 如何变成网表，与本讲的「版图美化」收尾形成首尾呼应。
- **U10-l2（RTL 到 GDSII 综合演练）**：把包括本讲在内的所有环节串成一次端到端实战，明确 logo 这类定制在整条交付链里的位置。
- **延伸阅读**：如果你想深入 SKILL，建议阅读 Cadence 官方 *SKILL Language Reference*，重点看 `let`/`procedure`/`prog`、collection 与 `db*`/`ge*`/`hi*` 三类 API；可以把本脚本改造成「画圆形 logo」「按文本字符串画字」等变体练手。
- **回看 u9-l1（芯片收尾）**：对照 filler cell、金属填充与本讲的 logo，体会「chip finishing」阶段「补全 + 美化 + 校验」三类操作的分工。
