# SPROM_CONT：系数地址与过采样时钟的核心生成逻辑

## 1. 本讲目标

本讲深入 [u4-l1](u4-l1-fir-coef-wrapper.md) 中那个被 FIR_COEF 当作黑盒使用的控制器 `SPROM_CONT`，把它打开，看清它内部到底做了三件事：

1. **奇偶抽头多相寻址**：用一个跟随 `LRCK_I` 的递增计数器，在一个 LRCK 周期内交替扫描「奇数系数地址」和「偶数系数地址」，对应 2 倍过采样的两相分解。
2. **反向地址映射**：把内部地址做按位取反后送给 ROM，使系数按卷积所需的倒序被读出。
3. **过采样时钟派生**：从地址计数器的不同位，直接派生出 `LRCKx_O`（= 2×LRCK）与 `BCKx_O`（= 2×BCK），无需任何外部 PLL。

学完本讲，你应当能够：

- 读懂 `CADDR_REG`、`CADDRO_REG`、`LRCKx_REG`、`BCKx_REG` 四个寄存器的递推关系，并能手算它们的波形。
- 解释「为什么地址计数器 + LRCK 最低位」天然产生奇偶交替的多相扫描。
- 说明 `LRCKx_O` 的一个完整周期为什么恰好等于「累加一个过采样样点」所需的时间。
- 理解 `BCKx_O` 在 `ROM_ADDR_WIDTH` 不同时为何要在「派生位」与「兜底 MCLK」之间二选一。

## 2. 前置知识

本讲假设你已掌握以下内容（来自前置讲义）：

- **过采样与多相分解的直觉**（[u1-l1](u1-l1-project-overview.md)）：2 倍过采样把每个输入样点变成 2 个输出样点；为此把一整套 FIR 系数拆成奇、偶两相，分别处理两个输出样点。
- **单时钟域 + 边沿检测**（[u2-l2](u2-l2-audio-clock-model.md)）：全设计只有 MCLK 一个时钟，BCK/LRCK 当作数据信号，用 `sig & ~sig_reg` 压成单周期脉冲来标记「新样点到来」。
- **FIR512 的命名耦合**（[u2-l2](u2-l2-audio-clock-model.md)）：1 个样点 = 512 个 MCLK；32 位立体声 I2S 帧下一个样点含 64 个 BCK。
- **三层封装套路**（[u3-l1](u3-l1-data-buffer-wrapper.md)、[u4-l1](u4-l1-fir-coef-wrapper.md)）：存储原语只管存取 → 控制器产生地址/使能 → 封装模块把二者绑定。本讲的 `SPROM_CONT` 正是系数侧的「控制器」。
- **FIR_COEF 的时钟对齐结论**（[u4-l1](u4-l1-fir-coef-wrapper.md)）：`SPROM_CONT` 输出的原始 `LRCKx_O`/`BCKx_O` 还要在 FIR_COEF 里再打 p1/p2 两拍，才变成对外的 `LRCKx2_O`/`BCKx2_O`。**本讲看到的 `LRCKx_O`/`BCKx_O` 是打拍之前的「原始版」**，不要与对外的 `LRCKx2_O` 混淆。

> 术语速查：ROM_ADDR_WIDTH = 系数 ROM 的地址位宽（默认 9，即 512 个系数）；CADDR = Coefficient Address（系数地址）；LRCKx/BCKx = 过采样后的 LRCK/BCK（频率为输入的 2 倍）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [03_SPROM_CONT/SPROM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v) | 被测设计（DUT）：本讲的主角，产生系数地址与过采样时钟。 |
| [03_SPROM_CONT/SPROM_CONT_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT_TB.v) | 测试激励：用 9 位计数器分频出 MCLK/BCK/LRCK，驱动 DUT 并 dump 波形。 |
| [03_SPROM_CONT/Questa/SPROM_CONT.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/Questa/SPROM_CONT.bat) | 仿真批处理：vlib/vlog/vsim 编译并启动仿真。 |
| [03_SPROM_CONT/Questa/run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/Questa/run.do) | 波形 do 文件：添加信号、run -all、出覆盖率报告。 |

模块边界一览（[SPROM_CONT.v:48-63](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L48-L63)）：

- 输入：`MCLK_I`、`BCK_I`、`LRCK_I`、`NRST_I`（低有效复位）。
- 输出：`CADDR_O`（系数地址，宽度 `ROM_ADDR_WIDTH`）、`LRCKx_O`、`BCKx_O`。
- 参数：`ROM_ADDR_WIDTH`（默认 9）。

> 一个准确但容易忽略的事实：`NRST_I` 在本模块**只出现在端口声明里，逻辑中从未使用**（你可以用 grep 自行确认）。所有寄存器都依赖 FPGA 上电初值（如 `reg BCKx_REG = 1'b1;`）。这与 [u2-l1](u2-l1-top-module-architecture.md) 提到的「FIR_COEF 复位端接恒高、永不复位」是一脉相承的设计——系数通路不需要受数据复位影响。

## 4. 核心概念与源码讲解

### 4.1 奇偶抽头多相寻址

#### 4.1.1 概念说明

2 倍过采样 FIR 的标准做法是**多相分解（polyphase decomposition）**：把一长串系数按索引的奇偶拆成两组——

- **奇相（odd phase）**：索引为 1, 3, 5, … 的系数，用来算第 1 个过采样样点；
- **偶相（even phase）**：索引为 0, 2, 4, … 的系数，用来算第 2 个过采样样点。

每来一个输入样点（一个 LRCK 周期），要顺序产出 2 个过采样样点，因此 ROM 要在一个 LRCK 周期内被完整扫描两遍：先扫奇相、再扫偶相。

`SPROM_CONT` 的巧妙之处在于：它没有用两个独立计数器，而是把**地址最低位直接绑到 `LRCK_I`**，用一个递增计数器 + 一个数据信号，就自然实现了「奇偶交替」。这正是本讲的核心机制。

#### 4.1.2 核心流程

地址寄存器 `CADDR_REG` 的更新规则（默认 `ROM_ADDR_WIDTH = W = 9`）：

- 在 `LRCK_I` 上升沿（新样点到来）：把 `CADDR_REG` 复位为初值 `000000001`。
- 其余每个 MCLK：高位段 `[W-1:1]` 加 1，最低位 `[0]` 直接跟随 `LRCK_I`。

用公式表达（`\{A, B\}` 表示拼接）：

\[
\text{CADDR\_REG} \;\leftarrow\; \big\{\,(\text{CADDR\_REG}[W\!-\!1:1] + 1),\ \text{LRCK\_I}\,\big\}
\]

由此可得地址的数值：

\[
\text{CADDR\_REG} = 2 \cdot \text{counter} + \text{LRCK\_I}
\]

其中 `counter` 是高位段那个每拍自增的计数器（8 位，会回绕）。由于 `LRCK_I` 在前半个周期为 1、后半个周期为 0，地址自然分成两段：

| 阶段（每个 256 MCLK） | `LRCK_I` | `counter` 走势 | `CADDR_REG` 序列 | 含义 |
|---|---|---|---|---|
| 前半（奇相） | 1 | 0 → 1 → … → 255 | 1, 3, 5, …, 511 | 扫描奇数系数地址 |
| 后半（偶相） | 0 | 0 → 1 → … → 255（回绕） | 0, 2, 4, …, 510 | 扫描偶数系数地址 |

也就是说，**最低位 = LRCK，自动完成了奇偶两相的切换**；高位计数器则在每个相位内顺序扫描 256 个地址（256 次乘加，对应一个过采样样点）。两相合计 512 次 MAC，正好填满一个 LRCK 周期（512 MCLK）。

#### 4.1.3 源码精读

边沿检测寄存器与地址寄存器声明（[SPROM_CONT.v:66-73](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L66-L73)）：`LRCK_REG` 用来缓存上一拍的 LRCK，做上升沿检测；`CADDR_REG` 即上面分析的地址寄存器。

地址更新主体在同一个 `always @(posedge MCLK_I)` 块里（[SPROM_CONT.v:76-94](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L76-L94)）。先看边沿检测与初值复位：

```verilog
BCK_REG  <= BCK_I;          // 缓存，做边沿检测
LRCK_REG <= LRCK_I;

if (LRCK_I & ~LRCK_REG == 1'b1) begin                 // 检测 LRCK 上升沿
    CADDR_REG <= {{(ROM_ADDR_WIDTH-1){1'b0}}, 1'b1};  // 复位为 ...00001
end
```

> 阅读小提示：`LRCK_I & ~LRCK_REG == 1'b1` 里，Verilog 中 `==` 的优先级**高于**二元 `&`，所以它实际等价于 `LRCK_I & ((~LRCK_REG) == 1'b1)`，而 `(~LRCK_REG == 1'b1)` 就是 `~LRCK_REG`。也就是说 `== 1'b1` 是冗余写法，整句即「LRCK 当前为 1、上一拍为 0」的上升沿检测——和 [u3-l2](u3-l2-dpram-ring-buffer-controller.md) 里 DPRAM_CONT 用的是同一套手法。

接着是奇偶切换的核心一句（[SPROM_CONT.v:86-88](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L86-L88)）：

```verilog
end else begin
    /* Change Odd & Even */
    CADDR_REG <= {(CADDR_REG[ROM_ADDR_WIDTH-1:1] + 1'b1), LRCK_I};
end
```

- `CADDR_REG[ROM_ADDR_WIDTH-1:1]` 取出高位计数器段；
- `+ 1'b1` 让它每拍自增（自然回绕）；
- 最低位拼成 `LRCK_I`，于是地址随 LRCK 电平在「全奇」「全偶」之间切换。

注释 `Change Odd & Even` 直白点明了设计意图。

#### 4.1.4 代码实践

**实践目标**：在仿真波形里亲眼确认「LRCK 高电平期间 `CADDR_REG` 全是奇数，低电平期间全是偶数」。

**操作步骤**：

1. 进入 `03_SPROM_CONT/Questa/`，运行 `SPROM_CONT.bat` 启动 Questa 仿真（它会把 `../*.v` 即 DUT 与 TB 一起编译，并执行 `run.do`）。
2. 默认的 [run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/Questa/run.do) 只把端口信号（`CADDR_O/LRCKx_O/BCKx_O`）加入了波形。要观察内部寄存器，需在 vsim 命令行手动追加：
   ```tcl
   add wave -position insertpoint sim:/SPROM_CONT_TB/u1/CADDR_REG
   add wave -position insertpoint sim:/SPROM_CONT_TB/u1/LRCK_I
   add wave -position insertpoint sim:/SPROM_CONT_TB/u1/LRCK_REG
   ```
   （`add log -r *` 已经把所有信号记录进波形库，所以这些内部信号可被直接添加。）
3. 把波形缩放到一个完整的 LRCK 周期（约 1024 ns，见 4.3 的频率计算），对齐到一次 LRCK 上升沿。

**需要观察的现象**：

- LRCK 上升沿那一拍，`CADDR_REG` 跳变为 `0x001`。
- 随后 LRCK 仍为高的约 256 拍里，`CADDR_REG` 依次为 `0x001, 0x003, 0x005, …, 0x1FF`（全奇）。
- LRCK 翻为低后，`CADDR_REG` 变为 `0x000, 0x002, 0x004, …, 0x1FE`（全偶）。

**预期结果**：奇偶分段清晰可见，且每段恰好 256 个地址。本讲给出的地址序列是**根据源码公式手算**得到的结论；波形中能否逐拍对上，需要本地验证。

> 注：`SPROM_CONT.bat`/`run.do` 是 Questa 专用脚本。若用 Icarus Verilog，可手动 `iverilog -g2012 -o sim.vvp ../*.v && vvp sim.vvp` 生成 `SPROM_CONT_TB.vcd`，再用 GTKWave 打开观察（TB 里已写 `$dumpfile/$dumpvars`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ROM_ADDR_WIDTH` 改成 8（系数 ROM 变成 256 项），地址分段会变成什么样？

**参考答案**：高位计数器段变成 7 位 `[7:1]`，每个相位扫描 \(2^{7}=128\) 个地址。前半段（LRCK=1）扫描 `1,3,…,253` 共 128 个奇地址；后半段（LRCK=0）扫描 `0,2,…,254` 共 128 个偶地址。一个 LRCK 周期仍被两相填满，只是每相的乘加数从 256 降为 128。

**练习 2**：为什么 `CADDR_REG` 的最低位可以直接用 `LRCK_I`，而不需要一个单独的「相位选择」寄存器？

**参考答案**：因为 `LRCK_I` 本身在前半周期恒为 1、后半周期恒为 0，正好等价于一个周期为 LRCK 的「相位标志」。把它拼进地址最低位，地址的奇偶性就自动跟随相位切换，无需额外状态——这是把「数据信号」兼作「控制信号」的典型单时钟域技巧（参见 [u2-l2](u2-l2-audio-clock-model.md)）。

---

### 4.2 反向地址映射

#### 4.2.1 概念说明

`CADDR_REG` 产生了一个「干净递增」的扫描地址，但它**并不直接送给 ROM**。真正出现在 `CADDR_O` 上的是 `CADDRO_REG`，而 `CADDRO_REG` 是 `CADDR_REG` 的**按位取反**（一补码）。这一步叫「反向地址映射」。

为什么需要反向？卷积的定义是：

\[
y[n] = \sum_{k} h[k]\cdot x[n-k]
\]

系数 \(h[k]\) 要与「时间上倒过来」的数据 \(x[n-k]\) 相乘。数据侧的环形缓冲（[u3-l2](u3-l2-dpram-ring-buffer-controller.md)）按某个固定方向扫描历史样点，于是系数侧必须按相反方向被读出，二者才能正确配对。控制器选择在「地址」这一层就做翻转，让 ROM 内部只需按自然顺序存放系数即可（系数文件如何排布见 [u6-l1](u6-l1-fir-coefficient-generation.md)）。

#### 4.2.2 核心流程

反向映射的定义（\(W =\) `ROM_ADDR_WIDTH`）：

\[
\text{CADDRO\_REG} = (2^{W}-1) - \text{CADDR\_REG} = \sim\text{CADDR\_REG}
\]

因为 \((2^{W}-1)\) 是 W 位全 1，减去一个 W 位数等价于按位取反。把 4.1 的 `CADDR_REG` 序列取反，得到真正访问 ROM 的地址序列：

| 阶段 | `CADDR_REG` 序列 | `CADDR_O = ~CADDR_REG` 序列（实际访 ROM） |
|---|---|---|
| 奇相 | 1, 3, 5, …, 511 | 510, 508, …, 2, 0（偶地址，递减） |
| 偶相 | 0, 2, 4, …, 510 | 511, 509, …, 3, 1（奇地址，递减） |

可以看到：ROM 在奇相访问的是「偶地址、由高到低」，在偶相访问的是「奇地址、由高到低」。系数在 `.hex` 文件里正是按这种能被倒序读出的方式预先排好的。

#### 4.2.3 源码精读

反向映射只有一行（[SPROM_CONT.v:91](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L91)）：

```verilog
CADDRO_REG <= {ROM_ADDR_WIDTH{1'b1}} - CADDR_REG;
```

`{ROM_ADDR_WIDTH{1'b1}}` 是 W 位全 1（即 \(2^{W}-1\)）。这行用减法实现取反，等价于 `~CADDR_REG`。随后在输出赋值里把 `CADDRO_REG` 接到端口（[SPROM_CONT.v:97](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L97)）：

```verilog
assign CADDR_O = CADDRO_REG;
```

注意 `CADDRO_REG` 与 `CADDR_REG` 在同一个 `always` 块里更新，因此 `CADDRO_REG` 比 `CADDR_REG` 晚一拍——但这恰好与下游 ROM 读延迟、乘法流水线的对齐需求一起被 FIR_COEF 的 p1/p2 打拍统一处理（见 [u4-l1](u4-l1-fir-coef-wrapper.md)）。

#### 4.2.4 代码实践

**实践目标**：验证「`CADDRO_REG` 永远等于 `CADDR_REG` 的按位取反」。

**操作步骤**：在 4.1.4 的波形基础上，再添加：

```tcl
add wave -position insertpoint sim:/SPROM_CONT_TB/u1/CADDRO_REG
```

**需要观察的现象**：任意时刻把 `CADDR_REG` 和 `CADDRO_REG` 的二进制位逐位比对，应互为 0/1。

**预期结果**：例如 `CADDR_REG = 0x001`（`000000001`）时，`CADDRO_REG = 0x1FE`（`111111110`）；`CADDR_REG = 0x1FF` 时，`CADDRO_REG = 0x000`。同时确认送到端口的 `CADDR_O` 与 `CADDRO_REG` 完全一致。

#### 4.2.5 小练习与答案

**练习 1**：把 `{ROM_ADDR_WIDTH{1'b1}} - CADDR_REG` 改写成 `~CADDR_REG`，行为会一样吗？

**参考答案**：在数学上完全一样（W 位全 1 减 x ≡ 按位取反 x），综合结果通常也一致。原写法用减法可能是为了在波形里更直观地体现「全 1 减某值」的反向语义；改写成 `~` 更简洁。两者等价。

**练习 2**：如果省掉这一层取反，直接把 `CADDR_REG` 送 ROM，会发生什么？

**参考答案**：地址不再倒序，奇相会正向访问 `1,3,…,511`、偶相访问 `0,2,…,510`。于是每个数据样点会乘到「时间上正向」的系数，卷积结果将错位/失真（实质上算出的是一组错相的滤波输出）。要让结果正确，要么保留这层取反，要么相应地重排 `.hex` 系数文件——两者必须配套。

---

### 4.3 过采样时钟派生

#### 4.3.1 概念说明

[u2-l2](u2-l2-audio-clock-model.md) 已经强调：过采样输出时钟 `LRCKx2`/`BCKx2` **不是外部 PLL 提供的**，而是芯片内部派生的。本讲揭示派生的具体位置——就在 `SPROM_CONT` 里，从系数地址计数器的不同位「顺手」取出。

关键直觉：地址计数器本质是一个以 MCLK 为节拍的二进制计数器，它的每一位都是一个 2 的幂次分频时钟。于是：

- 取计数器的「较高位」→ 分频比大 → 得到低频的 `LRCKx`；
- 取计数器的「较低位」→ 分频比小 → 得到较高频的 `BCKx`。

无需额外的分频器，地址计数器一身二任。

> 重要前提：这种派生假设了**标准的音频时钟比例关系**（MCLK = \(2^{W}\)·fs、32 位立体声即每样点 64 个 BCK）。模块并不去「测量」输入 BCK 的真实频率，而是按这套比例从 MCLK 反推。如果你喂入的 BCK 不符合该比例，`BCKx_O` 就不会正好是 2×BCK——这是设计内建的假设，移植时需留意。

#### 4.3.2 核心流程

**LRCKx（= 2×LRCK）的派生**：

\[
\text{LRCKx\_REG} = \text{CADDR\_REG}[W-1] = \text{counter 的最高位}
\]

W=9 时取 `CADDR_REG[8]`，即 8 位计数器的最高位 `counter[7]`。`counter[7]` 每 128 拍翻转一次，周期为 \(2^{8}=256\) 个 MCLK：

\[
T_{\text{LRCKx}} = 256\,T_{\text{MCLK}} = \tfrac{1}{2}\,T_{\text{LRCK}}
\]

而 LRCK 周期为 512 MCLK，故 `LRCKx` 频率恰为 LRCK 的 2 倍。

更关键的是周期与「一个过采样样点」的对应关系：一个过采样样点需要 256 次乘加（256 个 MCLK），而 \(T_{\text{LRCKx}}=256\,T_{\text{MCLK}}\)。所以——

> **`LRCKx_O` 的一个完整周期，正好等于累加一个过采样样点所需的时间。**

这里的「翻转一次对应一个样点」要精确理解：是 `LRCKx_O` 的**下降沿**（一次特定的翻转）触发下游 ADD 模块把累加和送出并清零（见 [u5-l2](u5-l2-add-accumulator.md)）；两次相邻下降沿之间完成 256 次乘加、产出 1 个样点。所以「每出现一次下降沿 → 输出一个过采样样点」成立。

**BCKx（= 2×BCK）的派生**：要更精细，需要根据 `ROM_ADDR_WIDTH` 选对计数器位。在 32 位立体声（每样点 64 个 BCK）前提下，BCK 与 MCLK 的关系为：

\[
f_{\text{BCK}} = 64\,f_s = 2^{6}\cdot \frac{f_{\text{MCLK}}}{2^{W}} = \frac{f_{\text{MCLK}}}{2^{W-6}}
\quad\Longrightarrow\quad
f_{2\times\text{BCK}} = \frac{f_{\text{MCLK}}}{2^{W-7}}
\]

而计数器位 `CADDR_REG[W-7]` 的周期正是 \(2^{W-7}\,T_{\text{MCLK}}\)，与 \(2\times\text{BCK}\) 完全吻合。因此：

\[
\text{BCKx\_REG} = \text{CADDR\_REG}[W-7]\quad(\text{当 } W\ge 8)
\]

W=9 时即 `CADDR_REG[2]`，周期 4 个 MCLK（= MCLK/4）；而 BCK = MCLK/8，故 2×BCK = MCLK/4，对上。

#### 4.3.3 源码精读

派生逻辑同样在那个 `always` 块里（[SPROM_CONT.v:92-93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L92-L93)）：

```verilog
LRCKx_REG  <= CADDR_REG[ROM_ADDR_WIDTH-1];                                // 取计数器最高位
BCKx_REG   <= (ROM_ADDR_WIDTH >= 8) ? CADDR_REG[ROM_ADDR_WIDTH-7] : 1'b0; // 取中段位
```

输出赋值（[SPROM_CONT.v:97-99](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L97-L99)）：

```verilog
assign LRCKx_O = LRCKx_REG;
assign BCKx_O  = (ROM_ADDR_WIDTH >= 7) ? BCKx_REG : MCLK_I; // Change BCK Generation
```

理解这两个阈值的分工：

- `LRCKx`：因为最高位总存在，所以**无条件**取 `CADDR_REG[W-1]`，无需兜底。
- `BCKx_REG` 的 `>= 8` 阈值：保证 `CADDR_REG[W-7]` 的索引 ≥ 1，即只取「真正的计数器位」，而不会误取最低位 `CADDR_REG[0]`（那一位装的是 `LRCK_I`，不是计数器）。当 \(W < 8\) 时索引进入了 LRCK 位，于是强制为 0。
- `BCKx_O` 的 `>= 7` 阈值：当地址足够宽（\(W\ge 7\)）时使用上面派生的 `BCKx_REG`；当地址太窄（\(W<7\)，计数器位数不足以产生有意义的 2×BCK 分频）时，**兜底直接输出 `MCLK_I`**。注释 `Change BCK Generation` 正是指这种按位宽切换生成方式的策略。

把两个阈值合起来看，三种典型情况：

| `ROM_ADDR_WIDTH` (W) | `BCKx_REG` 来源 | `BCKx_O` 取值 | 说明 |
|---|---|---|---|
| W ≥ 8（如默认 9） | `CADDR_REG[W-7]` | `BCKx_REG` | 派生出周期 \(2^{W-7}\) MCLK 的 2×BCK |
| W = 7 | 强制 `1'b0` | `BCKx_REG`（= 0） | 退化配置，输出恒 0（避免用到 LRCK 位） |
| W < 7 | 强制 `1'b0` | `MCLK_I`（兜底） | 地址过窄，直接用 MCLK |

> 这些 `BCKx` 是 SPROM_CONT 输出的「原始版」。如 [u4-l1](u4-l1-fir-coef-wrapper.md) 所述，FIR_COEF 会把它们再打 p1/p2 两拍对齐 ROM 读延迟后，才输出对外的 `BCKx2_O`。

#### 4.3.4 代码实践

**实践目标**：测量 `LRCKx_O` 与 `BCKx_O` 的周期，验证它们分别是 LRCK、BCK 的 2 倍频。

**操作步骤**：

1. 在波形中添加端口 `LRCKx_O`、`BCKx_O`，以及激励侧的 `LRCK_I`、`BCK_I`、`MCLK_I`（端口默认已在 `run.do` 中）。
2. 用光标测量相邻两次上升沿之间的时间。

**需要观察的现象**（基于 [TB](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT_TB.v) 的时钟：MCLK 周期 2 ns，`BCK_I=MCLK_REG[2]`，`LRCK_I=~MCLK_REG[8]`）：

| 信号 | 周期（手算） | 频率关系 |
|---|---|---|
| `MCLK_I` | 2 ns | 基准 |
| `BCK_I` | 16 ns（8 MCLK） | = MCLK/8 |
| `LRCK_I` | 1024 ns（512 MCLK） | = MCLK/512 |
| `LRCKx_O` | 256 ns（128 MCLK×2，即 256 MCLK） | = 2×LRCK |
| `BCKx_O` | 4 ns（2 MCLK×2，即 4 MCLK） | = 2×BCK |

> 关于 `LRCKx_O` 周期：它取 `CADDR_REG[8]`，周期 256 MCLK = 512 ns；但它在 LRCK 周期内的具体相位（在哪一拍翻转）取决于计数器与 LRCK 的对齐，需在波形中确认。「周期 = 512 ns = LRCK 的一半」是源码推得的结论，实际波形相位以本地验证为准。

**预期结果**：`LRCKx_O` 的周期约为 `LRCK_I` 的一半；`BCKx_O` 的周期约为 `BCK_I` 的一半。同时对照 4.1 的地址扫描，可以看到**每经历一个完整的 `LRCKx_O` 周期，ROM 恰好被扫描完一相（256 个系数）**——这正是「一个 LRCKx 周期 = 一个过采样样点」的波形佐证。

#### 4.3.5 小练习与答案

**练习 1**：默认 `ROM_ADDR_WIDTH=9` 时，`BCKx_O` 取自 `CADDR_REG[2]`。请解释为什么是第 2 位而不是别的位。

**参考答案**：在 32 位立体声下 BCK = MCLK/\(2^{W-6}\) = MCLK/\(2^{3}\) = MCLK/8，故 2×BCK = MCLK/4，周期 4 个 MCLK。计数器第 j 位（`CADDR_REG[j]`，j≥1）的周期是 \(2^{j}\) 个 MCLK；周期为 4 MCLK 的正是 j=2 即 `CADDR_REG[2]`。公式 `CADDR_REG[W-7]` 在 W=9 时也给出 2，二者一致。

**练习 2**：假如把音频格式从 32 位立体声换成 16 位立体声（每样点 32 个 BCK），`BCKx_O` 还会是 2×BCK 吗？

**参考答案**：不会。此时 BCK = 32·fs = MCLK/\(2^{W-5}\)，2×BCK = MCLK/\(2^{W-6}\)，对应的计数器位应是 `CADDR_REG[W-6]` 而非 `CADDR_REG[W-7]`。但源码写死了 `W-7` 这个偏移，所以 `BCKx_O` 会变成 2×（32 位格式下的 BCK），对 16 位格式而言频率就不对了。这印证了 4.3.1 提到的「派生假设了标准比例」——换格式需要同步调整这里的位选取。

---

## 5. 综合实践

**任务：把 SPROM_CONT 的「地址 + 时钟」三条输出，串成一张时序对照图，并据此解释一个过采样样点的完整诞生过程。**

具体步骤：

1. 在 Questa 中跑通 `03_SPROM_CONT/Questa/SPROM_CONT.bat`，按 4.1.4、4.2.4、4.3.4 把 `CADDR_REG`、`CADDRO_REG`、`LRCK_REG`、`LRCKx_O`、`BCKx_O`、`LRCK_I`、`MCLK_I` 全部加入波形。
2. 选取一个完整 LRCK 周期（约 1024 ns），在纸上画出这 7 个信号的对齐关系，标注：
   - LRCK 上升沿发生在哪一拍；
   - 奇相 256 拍内 `CADDR_REG` 与 `CADDR_O` 的首尾值；
   - 偶相 256 拍内的首尾值；
   - `LRCKx_O` 在该周期内的下降沿位置。
3. 结合本讲三个模块的结论，用一段话回答：**一个过采样样点是怎样被「地址扫描 + 系数读出 + 时钟节拍」三者协同产出的？** 要点应包括：256 次乘加对应一个相位、`CADDRO_REG` 保证系数倒序配对、`LRCKx_O` 的下降沿标志累加结束。

**预期成果**：一张标注完整的时序图 + 一段能把「多相寻址（4.1）→ 反向映射（4.2）→ 时钟派生（4.3）」三者贯穿起来的说明。波形中的精确相位以本地验证为准。

## 6. 本讲小结

- `SPROM_CONT` 用「高位计数器每拍自增 + 最低位跟随 `LRCK_I`」一行拼接，**自然实现了奇偶两相多相寻址**：LRCK 高电平扫奇地址、低电平扫偶地址，每相 256 次乘加。
- 真正送 ROM 的地址 `CADDR_O` 是内部地址的**按位取反** `CADDRO_REG = ~CADDR_REG`，用于把系数按卷积所需的倒序读出；`.hex` 系数排布必须与此配套。
- 过采样时钟**全由地址计数器的不同位派生**：`LRCKx_REG = CADDR_REG[W-1]`（周期 256 MCLK = LRCK/2），`BCKx_REG = CADDR_REG[W-7]`（周期 \(2^{W-7}\) MCLK = 2×BCK）。
- `LRCKx_O` 的一个完整周期（256 MCLK）恰好等于累加一个过采样样点所需的时间；其下降沿触发下游 ADD 输出并清零。
- `BCKx_O` 有两道 `ROM_ADDR_WIDTH` 阈值（`>=8` 选位、`>=7` 决定派生或兜底 MCLK），其正确性**前提是标准音频时钟比例**（MCLK = \(2^{W}\)·fs、32 位立体声）。
- `NRST_I` 在本模块声明但逻辑未用，寄存器全靠 FPGA 上电初值——系数通路不参与数据复位。

## 7. 下一步学习建议

- **向存储层下钻**：本讲的 `CADDR_O` 送给的是单口 ROM 原语 SPROM。下一篇 [u4-l3 SPROM 单口 ROM 原语](u4-l3-sprom-primitive.md) 讲它如何根据这个地址、经 `$readmemh` 加载的 `.hex` 输出系数，并对比数据侧的 SDPRAM。
- **向运算层延伸**：拿到系数后进入乘法与累加。建议接着读 [u5-l1 MULT 有符号乘法器](u5-l1-mult-pipeline.md) 与 [u5-l2 ADD 累加积分器](u5-l2-add-accumulator.md)，看 `LRCKx_O` 的下降沿如何驱动累加器复位、把本讲的「256 次乘加」收拢成一个样点。
- **回到系数源头**：想理解 `.hex` 里那些系数的奇偶排布与定点量化是如何生成的，可跳到 [u6-l1 FIR 系数生成](u6-l1-fir-coefficient-generation.md)，把「地址倒序读出」与「系数顺序写入」两端对接起来。
