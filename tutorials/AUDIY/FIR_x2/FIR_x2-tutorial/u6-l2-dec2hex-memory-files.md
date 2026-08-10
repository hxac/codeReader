# 十进制转十六进制：dec2hex.awk 与存储文件格式

## 1. 本讲目标

上一讲（u6-l1）我们用 Python 设计出了滤波器系数，并把它们保存成一个十进制文本文件 `FIR_filter_512taps_16bit.txt`。但硬件 ROM（`SPROM`）并不能直接读懂“带正负号的十进制数”——它需要的是**按地址排列、每个单元一个定宽十六进制数**的存储初始化文件。

本讲就解决这“最后一公里”：把十进制系数变成 ROM 能吃下去的十六进制文件。读完本讲你应当能够：

1. 看懂 `dec2hex.awk` 如何用**二补码（two's complement）**把有符号十进制数转成定宽十六进制，并能手算验证。
2. 说清楚 FIR_x2 里 `.hex` 存储文件的**格式约定**：每行一个值、多少位宽、多少行、和 ROM 深度的对应关系。
3. 区分 Questa 仿真用的 `.hex` 与硬件（Vivado 等）用的 `.data`，并知道为什么内容相同、扩展名却不同。

本讲只做“格式与数值转换”，不重新讨论滤波器设计（u6-l1）与运行时寻址（u4-l2）。

## 2. 前置知识

- **二补码（two's complement）**：计算机/FPGA 里表示有符号整数最常见的方式。一个 \(N\) 位二补码数能表示的范围是 \([-2^{N-1},\ 2^{N-1}-1]\)。负数 \(-x\) 的位图样等于 \(2^{N}-x\)。例如 16 位下 \(-1\) 的图样是 \(2^{16}-1=65535=\text{FFFF}\)。这是本讲的数学核心，不熟悉的读者先记住这一条。
- **`$readmemh`**：Verilog 系统任务，仿真开始时把一个文本文件里的十六进制数依次灌进存储器数组 `ROM[0], ROM[1], ...`。FIR_x2 的 `SPROM` 与 `SDPRAM_SINGLECLK` 都靠它加载初值（u4-l3、u3-l3）。
- **AWK**：一种逐行处理文本的小语言。`BEGIN{}` 在读文件前执行一次（用来做初始化），其后 `{}` 对输入的每一行执行一次。本讲的 `dec2hex.awk` 只有这两个块。
- **系数位宽与定标**：u6-l1 把系数量化成 16 位有符号整数（`BIT=16`），并在输出级丢掉低 14 位做定标。本讲处理的就是这 16 位的二补码十六进制表示。

> 约定：本讲“位宽”指一个系数的二进制位数（如 16）；“宽度 width”指 `dec2hex.awk` 参数里的**十六进制位数**（如 4）。二者关系是 \(\text{位宽}=\text{width}\times 4\)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [11_fir_gen/dec2hex.awk](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk) | 把有符号十进制 `.txt` 转成定宽二补码十六进制文件 | 全文精读：参数校验、二补码转换、格式化输出 |
| [11_fir_gen/fir_gen.py](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/fir_gen.py) | 生成十进制系数 `.txt`（u6-l1 已讲） | 只看它输出 `.txt` 的那一行，确认 dec2hex 的输入来源 |
| [11_fir_gen/README.md](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/README.md) | 系数生成流程说明 | 第 3 节：规定硬件用 `.data` 扩展名 |
| [08_hex/FIR512_x2_48000.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/08_hex/FIR512_x2_48000.hex) | 512 抽头系数的成品 `.hex`（参考输出） | 验证 dec2hex 的转换结果 |
| [08_hex/BUFFER_INIT.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/08_hex/BUFFER_INIT.hex) | 数据 RAM 的静音初始化文件 | 对照“位宽不同 ⇒ 十六进制位数不同” |
| [04_FIR_COEF/SPROM.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v) | 单口 ROM 原语 | 看 `$readmemh` 如何吃掉 `.hex` |
| [02_DATA_BUFFER/SDPRAM_SINGLECLK.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v) | 双口 RAM 原语 | 对照另一处 `$readmemh` 的用法 |

---

## 4. 核心概念与源码讲解

### 4.1 二补码十六进制转换

#### 4.1.1 概念说明

`dec2hex.awk` 解决的核心问题是：**怎样把一个可能为负的十进制整数，写成硬件 ROM 认得的二补码十六进制串？**

u6-l1 产出的系数是有符号十进制（比如 `-21`、`13658`）。ROM 单元本身没有“符号”概念，它只存一串 0/1。所以我们约定：用 16 位二补码来解释这串 0/1。于是负数要按二补码规则转成对应的“大正数图样”：

\[ \text{图样}(v) = \begin{cases} v & v \ge 0 \\ 2^{N} + v & v < 0 \end{cases} \]

其中 \(N=16\)。例如 \(v=-1 \Rightarrow 2^{16}+(-1)=65535=\text{FFFF}\)，\(v=-32768 \Rightarrow 32768=\text{8000}\)。这正是上一讲 u4-l3 里“`FFFF` 解码为 \(-1\)”的来源——同一套二补码约定。

转换之外还有两件格式琐事：每个数要**定宽**（不足位补零，如 `1` 写成 `0001`）且**大写**，否则 `$readmemh` 读出来位宽对不上、仿真出错。

#### 4.1.2 核心流程

`dec2hex.awk` 的整体逻辑：

```text
参数：width（十六进制位数）、out（输出文件名）、input.txt（每行一个十进制整数）

BEGIN（执行一次）：
  1. 校验 width>0、out 非空，否则报错退出
  2. max_val = 2^(width*4)        # 例如 width=4 → 2^16 = 65536
  3. half    = max_val / 2        # 注：计算了但未被使用（见 4.1.3）
  4. fmt     = "%0<width>X"       # 例如 "%04X"：零填充、4 位、大写十六进制

对输入每一行（执行多次）：
  5. dec = int($1)                # 读第一个字段为整数
  6. 若 dec<0：dec = max_val + dec  # 二补码：负数变正图样
  7. 若 dec 越界（>=max_val 或 <0）：警告并跳过
  8. hex = sprintf(fmt, dec)       # 格式化成定宽大写十六进制
  9. print hex >> out              # 追加写一行到输出文件
```

关键的二补码公式与位宽关系：

\[ \text{max\_val} = 2^{\text{width}\times 4},\qquad N = \text{width}\times 4 \]

\[ \text{图样}(v<0) = \text{max\_val} + v \]

#### 4.1.3 源码精读

脚本头部的用法注释直接给出了标准调用方式：

[11_fir_gen/dec2hex.awk:L1-L2](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L1-L2) —— `-v width=4 -v out="output.data"` 通过命令行把 `width` 和 `out` 两个变量传进 awk，这是整个脚本的唯二可配置项。

`BEGIN` 块负责一次性初始化与参数校验：

[11_fir_gen/dec2hex.awk:L3-L16](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L3-L16) —— 先检查 `width`/`out` 是否合法（不合法就 `print ... > "/dev/stderr"` 报错并 `exit 1`），再算出三个派生量。其中：

[11_fir_gen/dec2hex.awk:L13-L15](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L13-L15) —— `max_val = 2^(width*4)` 是二补码的模（16 位时为 65536）；`fmt = "%0" width "X"` 是 awk 字符串拼接，`width=4` 时得到 `"%04X"`（零填充、最小宽度 4、大写十六进制）。

> **精度提醒**：第 14 行的 `half = max_val / 2` 被计算出来，但整个脚本再没有引用过它（主处理块只用 `max_val`、`fmt`）。它是一段**遗留的未使用代码**，不影响功能，读源码时不必为它纠结。

真正逐行处理输入的是主块：

[11_fir_gen/dec2hex.awk:L18-L33](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L18-L33) —— 三段逻辑依次为“读数 → 二补码转换 → 越界保护 → 格式化输出”。其中二补码转换只有一行：

[11_fir_gen/dec2hex.awk:L22-L24](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L22-L24) —— `if (dec < 0) dec = max_val + dec;` 正是上面的公式 \(\text{max\_val}+v\)，把负数映射成无符号图样。

[11_fir_gen/dec2hex.awk:L26-L29](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L26-L29) —— 越界保护：转换之后再判 `dec >= max_val || dec < 0`。注意它校验的是**无符号 \(N\) 位范围** \([0,\text{max\_val})\)。对于过大的负数（如 `-70000`）转换后仍小于 0，会触发警告；对于过大的正数（如 `70000`）则直接越界。但对于落在 \([2^{N-1},\ 2^{N})\) 的正数（如 `40000`），脚本**不会**警告——它会静默输出 `9C40`，而这在 16 位二补码里其实代表负数。因此“正确性”依赖上游 u6-l1 保证每个系数都落在有符号 16 位范围 \([-32768,\ 32767]\) 内；`fir_gen.py` 的 `MAX_TOTAL` 约束在实践中保证了这一点。

最后是格式化与写文件：

[11_fir_gen/dec2hex.awk:L31-L32](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/dec2hex.awk#L31-L32) —— `sprintf(fmt, dec)` 把整数变成定宽大写十六进制串；`print hex >> out` 用追加方式写入。**注意 `>>` 是追加写**：在同一个 awk 进程内多次执行该行会累加（这是期望行为，逐行写满整个文件）；但如果对同一个 `out` 文件**连续运行两次脚本**，结果会翻倍、文件损坏。重新生成前应先删除旧文件。

下面是用 `width=4` 手算的几组对照，可与下一节的成品 `.hex` 互相印证：

| 输入十进制（有符号） | `max_val+dec` 转换 | `%04X` 输出 |
| --- | --- | --- |
| `-1` | \(65536-1=65535\) | `FFFF` |
| `-2` | \(65534\) | `FFFE` |
| `-21` | \(65515\) | `FFEB` |
| `-32768` | \(32768\) | `8000` |
| `0` | \(0\) | `0000` |
| `1` | \(1\) | `0001` |
| `13658` | \(13658\) | `355A` |
| `32767` | \(32767\) | `7FFF` |

#### 4.1.4 代码实践

**目标**：手工跑一遍 `dec2hex.awk` 的转换逻辑，确认它与成品 `.hex` 完全一致。

**操作步骤**：

1. 用一行简易输入模拟脚本（无需装 Python/scipy）。把下面三个数写进 `mini.txt`：
   ```text
   1
   -1
   13658
   ```
2. 运行脚本（`width=4`、输出 `mini.data`）：
   ```bash
   awk -v width=4 -v out="mini.data" -f 11_fir_gen/dec2hex.awk mini.txt
   ```
3. 查看 `mini.data`。

**预期结果**（待本地验证，因为命令需在你自己的 shell 中执行）：

```text
0001
FFFF
355A
```

**需要观察的现象**：
- `-1` 变成了 `FFFF`，`1` 变成了 `0001`，二者互为按位取反——这是二补码的标志。
- `13658` 输出 `355A`，正好等于 [08_hex/FIR512_x2_48000.hex:L255-L259](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/08_hex/FIR512_x2_48000.hex#L255-L259) 里那个**滤波器主瓣峰值** `355A`（低通 FIR 中心抽头最大，符合 u6-l1 的预期）。
- 每一行都恰好 4 个字符、不足补零、全大写。

如果你再故意写入一个越界值（如 `70000`），stderr 应打印 `Warning: Value out of range at line N: 70000`，且该行不会写进 `mini.data`。

#### 4.1.5 小练习与答案

**练习 1**：用 `width=3` 运行脚本，输入 `-1`，输出会是什么？这对应几位二补码？

**答案**：`width=3` 时 `max_val=2^(3*4)=4096`，`-1 → 4095 → sprintf("%03X",4095)="FFF"`，对应 12 位二补码，范围 \([-2048,\ 2047]\)。

**练习 2**：脚本为什么用 `dec = max_val + dec` 而不是 `dec = ~(-dec)` 之类按位取反？提示：awk 的数是什么类型？

**答案**：awk 把数值当**浮点数**处理，没有固定位宽的整数按位运算语义。`max_val + dec` 是纯算术，显式以 \(2^N\) 为模，行为与位宽 `width` 严格对应、可预测；而 `~` 在 awk 里是浮点按位运算，位宽不明确，容易出错。

**练习 3**：`half` 变量为何是“多余”的？如果你来维护这个脚本，可以怎么处理？

**答案**：`half = max_val/2` 在后续从未被读取，是死代码。可以删除它；也可以在注释里说明它原本可能用于“正负边界判断”但最终没采用，留给后人参考。

---

### 4.2 存储文件格式与位宽

#### 4.2.1 概念说明

转换出来的十六进制数还要按一定**格式**排进文件，`$readmemh` 才能正确地“第 1 行 → ROM[0]、第 2 行 → ROM[1] ……”依次灌入。FIR_x2 的存储文件格式非常朴素：

- **每行一个值**，纯十六进制，没有任何地址标注、没有 `radix` 前缀、没有逗号。
- **行数 = ROM 深度**（`2^ADDR_WIDTH`）。
- **每行位数 = 数据位宽 / 4**（一个十六进制字符代表 4 位）。

这一节要弄清三件事：行数怎么来、每行几位、以及**系数文件与数据 RAM 文件位宽不同**带来的区别。

#### 4.2.2 核心流程

ROM 深度与位宽都从模块参数派生（u4-l3）：

\[ \text{MEMORY\_DEPTH} = 2^{\text{ADDR\_WIDTH}},\qquad \text{每行十六进制位数} = \text{DATA\_WIDTH}/4 \]

FIR_x2 里有两类存储文件，对照如下：

| 文件 | 喂给谁 | DATA_WIDTH | ADDR_WIDTH | 深度（行数） | 每行十六进制位数 |
| --- | --- | --- | --- | --- | --- |
| `FIR512_x2_48000.hex` | `SPROM`（系数 ROM） | 16（COEF_WIDTH） | 9（RADDR_WIDTH） | \(2^9=512\) | 4 |
| `FIR256_x2_48000.hex` | `SPROM`（更短滤波器） | 16 | 8 | \(2^8=256\) | 4 |
| `FIR128_x2_48000.hex` | `SPROM`（更短滤波器） | 16 | 7 | \(2^7=128\) | 4 |
| `BUFFER_INIT.hex` | `SDPRAM`（输入数据 RAM） | 32（DATA_WIDTH） | 8（WADDR_WIDTH） | \(2^8=256\) | 8 |

`$readmemh` 的读取规则：它从文件第 1 行开始，依次填入 `ROM[0]`、`ROM[1]`、……，直到数组填满或文件读完。文件里也可以用 `@地址` 跳转、用 `x`/`z` 表示未知，但 FIR_x2 的文件**都没有用这些**，就是最朴素的顺序填充。

> **命名规律**：`FIR{N}_x2_{fs}` 中 `N` 就是抽头数 = ROM 深度 = `MCLK/fs`（如 `FIR512` 对应 \(24.576\,\text{MHz}/48\,\text{kHz}=512\)，呼应 u2-l2 的 `FIR512` 命名耦合）；`x2` 是过采样比；`48000` 是目标采样率。

#### 4.2.3 源码精读

先看系数文件本体。开头是大量 `0000`（滤波器两端的抽头接近 0），中间穿插小正负值：

[08_hex/FIR512_x2_48000.hex:L1-L8](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/08_hex/FIR512_x2_48000.hex#L1-L8) —— 第 1 行 `0000` 正是 u6-l1 里 `np.insert(coef_int, 0, 0)` 补进去的那个**前导零抽头**（落在偶地址分支 `coef_int[0::2]`）。第 23 行的 `FFFF` 就是 \(-1\)，印证二补码转换。

[08_hex/FIR512_x2_48000.hex:L255-L259](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/08_hex/FIR512_x2_48000.hex#L255-L259) —— 文件正中央（第 257 行）是 `355A`，两侧对称地出现 `275A`、`0A2F`。这正是线性相位低通 FIR 的典型形状：中心抽头最大、向两端对称衰减。整文件共 512 行，与 `SPROM` 的 `MEMORY_DEPTH=2^9=512` 严丝合缝。

再看“位宽不同 ⇒ 每行位数不同”的对照——数据 RAM 的初始化文件：

[08_hex/BUFFER_INIT.hex:L1-L2](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/08_hex/BUFFER_INIT.hex#L1-L2) —— 每行 8 个十六进制字符（`00000000`），对应 `SDPRAM` 的 `DATA_WIDTH=32`；共 256 行，对应 `ADDR_WIDTH=8`、深度 \(2^8=256\)。全 0 表示“上电静音初始化”。这与系数文件 4 位/行形成鲜明对比：**同样的 `$readmemh`，只因 `DATA_WIDTH` 不同，文件每行位数就不同**。

最后看消费这些文件的原语。系数 ROM 的加载：

[04_FIR_COEF/SPROM.v:L64-L67](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L64-L67) —— `initial $readmemh(ROM_INIT_FILE, ROM);` 在时刻 0 无条件把文件灌进 `ROM`。`ROM` 声明为 `reg [DATA_WIDTH-1:0] ROM[MEMORY_DEPTH-1:0]`（[SPROM.v:L57-L62](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L57-L62)），所以文件行数必须等于 `MEMORY_DEPTH`，否则仿真会有未初始化单元。

数据 RAM 的加载多了一道空串判断（可选初始化）：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:L73-L78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L73-L78) —— `if (RAM_INIT_FILE != "") $readmemh(...)`，说明数据 RAM 的初始化是**可选**的（可以传空串跳过），而系数 ROM 是**强制**的——因为滤波器系数上电即固定，数据 RAM 却会被运行时不断写入。

> **澄清一个可能的误解**：上一讲结尾提到系数“须按 SPROM_CONT 反向地址重排后才能写入 ROM”。实地读 `dec2hex.awk` 可以确认：**它逐行 1:1 转换，完全不做地址重排**，`.hex` 的顺序与 `fir_gen.py` 输出的 `.txt` 完全一致（自然下标顺序，第 1 行就是 `coef_int[0]`）。系数在 ROM 里“落点是否正确”是由 `SPROM_CONT` 在**运行时**用 `CADDRO_REG = ~CADDR_REG` 的镜像地址扫描来保证的（见 u4-l2），生成流程本身不需要任何重排脚本。换言之：地址映射是硬件运行时的事，文件只是按自然顺序老老实实存系数。

#### 4.2.4 代码实践

**目标**：用数行命令核验“行数 = 深度、每行位数 = 位宽/4”这两条规律。

**操作步骤**：

1. 统计各系数文件的非空行数：
   ```bash
   grep -c . 08_hex/FIR512_x2_48000.hex   # 期望 512
   grep -c . 08_hex/FIR256_x2_48000.hex   # 期望 256
   grep -c . 08_hex/FIR128_x2_48000.hex   # 期望 128
   grep -c . 08_hex/BUFFER_INIT.hex        # 期望 256
   ```
2. 统计每行字符宽度（取第一行长度）：
   ```bash
   awk 'NR==1{print length($1)}' 08_hex/FIR512_x2_48000.hex   # 期望 4
   awk 'NR==1{print length($1)}' 08_hex/BUFFER_INIT.hex        # 期望 8
   ```

**预期结果**（待本地验证）：
- 三个系数文件行数分别为 512 / 256 / 128，正好是 \(2^9/2^8/2^7\)。
- 系数文件每行 4 个字符（16 位），`BUFFER_INIT.hex` 每行 8 个字符（32 位）。

**需要观察的现象**：
- 行数随滤波器长度（`FIR128/256/512`）成倍变化，而每行位数始终是 4（因为 `COEF_WIDTH` 恒为 16）。
- `BUFFER_INIT.hex` 行数与 `FIR256` 相同（都是 256），但每行位数翻倍——证明“行数由地址位宽决定、每行位数由数据位宽决定”，二者独立。

#### 4.2.5 小练习与答案

**练习 1**：如果要做一个 1024 抽头、系数仍为 16 位的滤波器，`.hex` 文件该有多少行、每行几位？`SPROM` 的 `ADDR_WIDTH` 应设为多少？

**答案**：1024 行（\(2^{10}\)）、每行仍 4 位（`COEF_WIDTH=16`）；`ADDR_WIDTH=10`。相应地顶层 `WADDR_WIDTH=9`（因为 `RADDR_WIDTH=WADDR_WIDTH+1`）。

**练习 2**：为什么 `BUFFER_INIT.hex` 全 0 也行？把它改成全 `FFFFFFFF` 会怎样？

**答案**：全 0 表示每个 RAM 单元初值为 `0`，即“静音”。数据 RAM 在运行时会被 `DPRAM_CONT` 不断写入真实 PCM 样点（u3-l2），初值只在复位预填充阶段短暂出现。若改成全 `FFFFFFFF`（即 `-1`），上电瞬间 RAM 里会是一串满幅负样点，复位结束前可能产生一个“咔哒”声脉冲，但正常运行后会被覆盖；功能上不致命，但不推荐。

**练习 3**：`$readmemh` 与 `$readmemb` 的区别是什么？为什么 FIR_x2 用前者？

**答案**：`$readmemb` 按**二进制**解析文件（每行是一串 0/1），`$readmemh` 按**十六进制**解析。FIR_x2 的文件每行是十六进制（如 `355A`），所以必须用 `$readmemh`；若误用 `$readmemb`，`355A` 会被当成非法二进制字符而报错或得到 `xxxxxxxx`。

---

### 4.3 厂商文件差异（hex/data）

#### 4.3.1 概念说明

`.hex` 和 `.data` 的**内容格式其实完全一样**——都是“每行一个定宽大写十六进制数”。区别只在**扩展名**与**谁来读它**：

- **`.hex`**：Questa 仿真里 `$readmemh` 加载，扩展名是 FIR_x2 的仿真约定。仓库里 `08_hex/` 存放成品母本，各模块的 `Questa/` 子目录各取所需。
- **`.data`**：硬件流程（Vivado/AMD 等厂商工具初始化 Block ROM/RAM）用，由 `dec2hex.awk` 按需生成。`11_fir_gen/README.md` 第 3 节明确要求硬件输出文件用 `.data` 扩展名。

之所以要分两个名字，本质是**工具链约定**：`$readmemh` 对扩展名不挑剔（它只看内容是不是合法十六进制），但厂商综合/初始化流程往往按扩展名或按各自的 BRAM 初始化工具来识别文件。所以同一份系数，仿真用 `.hex`、下板用 `.data`，内容由同一个 `dec2hex.awk` 保证一致。

#### 4.3.2 核心流程

两类文件的来源与去向：

```text
fir_gen.py ──► FIR_filter_512taps_16bit.txt   （十进制，每行一个有符号整数）
                        │
                        ▼  dec2hex.awk  (-v width=4)
                        ├──► *.hex   ──► Questa 仿真：$readmemh 灌进 SPROM/SDPRAM
                        └──► *.data  ──► 厂商硬件：Block ROM/RAM 初始化（Vivado 等）
```

仓库里现成的成品分布：

- `08_hex/`：母本仓库，含 `BUFFER_INIT.hex` 与 `FIR128/256/512_x2_48000.hex`。
- 各模块 `Questa/` 子目录：存放仿真实际加载的副本。例如 `04_FIR_COEF/Questa/FIR512_x2_48000.hex` 与 `08_hex/FIR512_x2_48000.hex` **逐字节相同**（可用 `diff` 验证），是同一份文件的拷贝。
- `02_DATA_BUFFER/Questa/BUFFER_INIT.hex`：同样是从 `08_hex/` 复制来的数据 RAM 初值。

#### 4.3.3 源码精读

权威依据在工具自身的说明文档里：

[11_fir_gen/README.md:L30-L45](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/README.md#L30-L45) —— 第 3 节“Convert to Hexadecimal Format”规定了三件事：输出必须是**有符号二补码十六进制**、十六进制位数可配置（16 位用 4 位）、**输出文件扩展名应为 `.data`**。

[11_fir_gen/README.md:L34-L37](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/README.md#L34-L37) —— “Requirements”三条，明确点出 `.data` 扩展名与“可配置十六进制位数（4 digits for 16 bits）”。

[11_fir_gen/README.md:L41-L43](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/README.md#L41-L43) —— 给出的标准命令：

```bash
awk -v width=4 -v out="FIR_filter.data" -f dec2hex.awk FIR_filter_512taps_16bit.txt
```

注意 `out="FIR_filter.data"` 直接把扩展名定成了 `.data`。`dec2hex.awk` 本身对扩展名无所谓（它只是把 `out` 当文件名用），**扩展名约定来自 README，而非脚本**。

再回到仿真侧，确认 `.hex` 是谁在用：

[04_FIR_COEF/SPROM.v:L64-L67](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/SPROM.v#L64-L67) —— Questa 里 `$readmemh(ROM_INIT_FILE, ROM)`，`ROM_INIT_FILE` 由顶层经 `FIR_COEF` 传下来；顶层 [07_FIR_x2/FIR_x2.v:L52-L59](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L52-L59) 的参数 `COEF_INIT` 默认指向 `.hex` 文件，数据侧的 `BUFF_INIT`（[FIR_x2.v:L79](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L79)）也是 `.hex`。所以**仿真链路全用 `.hex`**。

把两侧对照成表：

| 维度 | Questa 仿真（`.hex`） | 硬件流程（`.data`） |
| --- | --- | --- |
| 内容格式 | 每行一个定宽大写十六进制 | 每行一个定宽大写十六进制（**相同**） |
| 谁来读 | Verilog `$readmemh` | 厂商 BRAM 初始化工具（Vivado 等） |
| 怎么得到 | 仓库现成（`08_hex/`、各 `Questa/`） | 由 `dec2hex.awk` 现场生成 |
| 扩展名来源 | FIR_x2 仿真约定 | `11_fir_gen/README.md` §3 规定 |
| 顶层参数 | `COEF_INIT="FIR512.hex"` 等 | 移植时改传 `.data` 文件名 |

> **移植提示**：把 FIR_x2 搬到 Vivado/AMD 时，原语 `SPROM`/`SDPRAM` 要换成厂商 Block ROM/RAM IP（u6-l4 会详谈），系数文件也从 `.hex` 换成 `.data`。文件**内容不用重写**（格式一致），只需用 `dec2hex.awk` 重新生成并改扩展名，再在厂商 IP 里指向它即可。具体某家厂商工具识别 `.data` 的细节属于工具链范畴，**待确认**，以各厂商文档为准。

#### 4.3.4 代码实践

**目标**：用 `dec2hex.awk` 把（模拟的）十进制系数转成 4 位宽 `.data` 文件，并说明在 Questa 与 Vivado 两种工具下分别该用哪种扩展名。

**操作步骤**：

1. 准备一个迷你十进制系数文件 `mini_coef.txt`（模拟 `fir_gen.py` 的输出）：
   ```text
   0
   1
   -1
   13658
   -13658
   ```
   （第 1 行的 `0` 模拟 u6-l1 补进去的前导零抽头。）
2. 生成 `.data` 文件（`width=4`）：
   ```bash
   awk -v width=4 -v out="mini_coef.data" -f 11_fir_gen/dec2hex.awk mini_coef.txt
   cat mini_coef.data
   ```
3. 用**完全相同**的命令、只改 `out` 扩展名，再生成一份 `.hex`：
   ```bash
   awk -v width=4 -v out="mini_coef.hex" -f 11_fir_gen/dec2hex.awk mini_coef.txt
   diff mini_coef.data mini_coef.hex
   ```

**预期结果**（待本地验证）：
- `mini_coef.data` 内容：
  ```text
  0000
  0001
  FFFF
  355A
  CAA6
  ```
  其中 `CAA6` 是 \(-13658\) 的二补码（\(65536-13658=51878=\text{CAA6}\)），与 `355A` 互为按位取反，符合预期。
- `diff mini_coef.data mini_coef.hex` **没有任何输出**——证明二者内容逐字节相同，只是扩展名不同。

**需要观察的现象与结论**：
- 同一份系数，`.data` 与 `.hex` 内容完全一致，验证“内容相同、扩展名不同”。
- **Questa 仿真**：应使用 `.hex`（顶层 `COEF_INIT` 默认就是 `.hex`，`$readmemh` 加载）。
- **Vivado/硬件**：应使用 `.data`（按 `README.md` §3 的规定，供厂商 BRAM 初始化工具读取）。
- 如果手头有真实的 `fir_gen.py` 输出，把上面 `mini_coef.txt` 换成 `FIR_filter_512taps_16bit.txt`，生成结果应与 `08_hex/FIR512_x2_48000.hex` 逐行一致（这正是“成品 `.hex` 即该流程的参考输出”的强校验）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dec2hex.awk` 不直接把输出文件名写死成 `.hex` 或 `.data`，而要用 `-v out=...` 传入？

**答案**：因为同一份系数既要给仿真（`.hex`）又要给硬件（`.data`），内容一样、扩展名不同。把文件名做成参数，一个脚本就能服务两条链路，避免维护两份逻辑相同的代码。

**练习 2**：如果你在 Questa 仿真时误把一个 `.data` 文件名传给 `COEF_INIT`，会发生什么？

**答案**：**大概率正常工作**。`$readmemh` 只按内容解析十六进制，不看扩展名；只要文件内容格式正确、行数与 ROM 深度匹配，仿真正常。扩展名 `.data`/`.hex` 对 `$readmemh` 无意义，它只是给人和其它工具看的约定。这也正是“内容相同、扩展名不同”能成立的根本原因。

**练习 3**：把 FIR_x2 移到 Vivado 时，除了换 `.data` 文件，原语层面还要做什么？简述原因。

**答案**：要把 `SPROM`/`SDPRAM_SINGLECLK` 这两个行为级原语替换成 Vivado 的 Block ROM/RAM IP（或其推断模板）。原因是厂商综合工具对 Block RAM 的初始化方式、寄存器级数、时序模型与通用 `$readmemh` 行为级模型不完全相同；用厂商 IP 能保证资源（真 BRAM）、时序和初始化文件（`.data`/`.coe` 等）被正确识别。详见 u6-l4。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次“从十进制系数到可下板文件”的完整流程，并与仓库成品交叉验证。

**任务**：重现 `FIR512_x2_48000.hex` 的生成，并确认它在仿真与硬件两条链路下分别叫什么名字。

**步骤**：

1. **生成十进制系数**（依赖 `numpy`/`scipy`/`matplotlib`，运行环境待本地确认）：
   ```bash
   cd 11_fir_gen
   python fir_gen.py
   ```
   预期在当前目录生成 `FIR_filter_512taps_16bit.txt`（512 行十进制整数，第 1 行为 `0`）。同时终端会打印 Odd/Even 抽头和的溢出检查结果（应均为 `[OK ( <= 16383 )]`，呼应 u6-l1 的 `MAX_TOTAL`）。

2. **转成十六进制**，分别产出 `.hex` 与 `.data`：
   ```bash
   awk -v width=4 -v out="FIR512_x2_48000.hex"  -f dec2hex.awk FIR_filter_512taps_16bit.txt
   awk -v width=4 -v out="FIR512_x2_48000.data" -f dec2hex.awk FIR_filter_512taps_16bit.txt
   ```

3. **与成品逐行比对**：
   ```bash
   diff FIR512_x2_48000.hex ../08_hex/FIR512_x2_48000.hex && echo "MATCH"
   diff FIR512_x2_48000.hex FIR512_x2_48000.data && echo "SAME_CONTENT"
   ```

**预期结果**（待本地验证）：
- 第 3 步第一条 `diff` 无输出并打印 `MATCH`：证明你重现的系数与仓库成品**完全一致**，整条 `fir_gen.py → dec2hex.awk` 链路自洽。
- 第二条 `diff` 无输出并打印 `SAME_CONTENT`：证明 `.hex` 与 `.data` 内容相同。

**需要观察与思考的现象**：
- 在第 2 步之前，若 `FIR512_x2_48000.hex` 已存在，由于 `print >> out` 是追加写，必须先 `rm` 旧文件，否则行数会翻倍（参见 4.1.3 的提醒）。
- 得到的文件：**Questa 仿真**用 `.hex`（顶层 `COEF_INIT` 默认值），**Vivado/硬件**用 `.data`（按 README §3）。
- 抽查文件第 257 行应为 `355A`（中心抽头峰值），第 1 行应为 `0000`（前导零抽头）——与 4.2 的观察一致。

> 如果本机没有 Python/scipy 环境，可用 4.1.4 或 4.3.4 的迷你文件替代第 1 步，重点放在第 2、3 步对 `dec2hex.awk` 转换与文件格式的验证上。

## 6. 本讲小结

- `dec2hex.awk` 用**二补码**把有符号十进制转成定宽大写十六进制：负数 \(v\) 映射为 \(\text{max\_val}+v\)，其中 \(\text{max\_val}=2^{\text{width}\times 4}\)；`width=4` 对应 16 位系数。
- 它逐行 **1:1 转换、不做任何地址重排**；`.hex` 的顺序与 `fir_gen.py` 的 `.txt` 完全一致，系数落点由 `SPROM_CONT` 运行时镜像寻址保证（u4-l2）。
- 存储文件格式极朴素：每行一个值、行数 = ROM 深度 \(2^{\text{ADDR\_WIDTH}}\)、每行位数 = `DATA_WIDTH/4`；系数文件 4 位/行，数据 RAM 文件 8 位/行。
- `.hex`（Questa `$readmemh` 仿真）与 `.data`（Vivado 等硬件初始化）**内容格式完全相同**，区别仅在扩展名与读取者，由 `11_fir_gen/README.md` §3 约定。
- `$readmemh` 本身对扩展名不敏感；移植到 Vivado 时只需用 `dec2hex.awk` 重新生成 `.data`、把原语换成厂商 Block ROM/RAM IP，文件内容无需重写。
- 成品 `08_hex/FIR512_x2_48000.hex` 就是 `fir_gen.py → dec2hex.awk` 的参考输出，可用 `diff` 强校验整条生成链路。

## 7. 下一步学习建议

- **横向验证**：结合 u6-l3（PSL 断言与覆盖率），用 `report.txt` 确认整条数据通路的分支覆盖，从而间接确认这些 `.hex` 系数在仿真中被正确读取并参与了运算。
- **纵向移植**：进入 u6-l4（FPGA 移植与开发板示例），亲手把 `SPROM`/`SDPRAM` 换成某厂商 Block ROM/RAM IP，并用本讲生成的 `.data` 初始化它，完成“系数字面量 → 真实 BRAM”的闭环。
- **回归寻址**：若对“为什么不重排也能正确卷积”仍有疑问，回头重读 u4-l2 的 `CADDR_REG`/`CADDRO_REG = ~CADDR_REG` 镜像扫描，并把本讲的 `.hex` 行号与 `SPROM_CONT` 的地址波形对照，看清系数是如何被“按需取出”的。
