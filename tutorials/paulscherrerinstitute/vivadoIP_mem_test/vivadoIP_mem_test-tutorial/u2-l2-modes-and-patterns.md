# 测试模式与数据 pattern

## 1. 本讲目标

学完本讲，你应该能够：

- 说清四种**运行模式**（Single / Continuous / WriteOnly / ReadOnly）各自的触发方式、执行路径和停止方式，特别是 Continuous 的迭代计数与 STOP 的「优雅停止」语义。
- 说清四种**数据 pattern**（Counter / Walking-1 / OwnAddress / PseudoRandom）的生成规则与适用场景。
- 把模式常量、pattern 常量与 `mem_test.vhd` 里的状态机分支、pattern 初始化/更新代码对应起来。
- 面对一个真实调试需求（数据线粘连、长时间压力测试等），能选出合适的 pattern + mode 组合并说出理由。

本讲承接 [u2-l1 寄存器地图](u2-l1-register-map.md)：那讲确立了「`REG_MODE`（0x0C）和 `REG_PATTERN_SEL`（0x20）是两个配置型寄存器」这一契约，本讲就回答「这两个寄存器里到底可以写什么值、写进去之后硬件会怎么跑」。本讲**不**深入 pattern 读回比对的字节地址换算细节（那是 [u3-l4](u3-l4-pattern-generation-and-check.md) 的主题），只讲清楚模式与 pattern 的语义和生成规则。

## 2. 前置知识

- **模式（mode）**：一次内存测试的「运行剧本」——要不要先写、要不要再读、跑完一遍停不停。它决定 IP 跑多长时间、走哪些状态。
- **pattern（测试图形）**：写到被测存储器里、再读回来比对的「参考数据」。pattern 不同，能暴露的故障类型就不同。
- **beat（拍）**：AXI 数据总线上一次握手传输的一个数据字。本 IP 里 pattern 是逐拍生成、逐拍写、逐拍读回比对的。一段测试范围 `SIZE` 字节会被换算成若干 beat（详见 [u4-l2](u4-l2-axi4-master.md)）。
- **stuck-at 故障**：某根数据线/地址线被「粘」在高电平（stuck-at-1）或低电平（stuck-at-0），无论你写什么它都读出固定值。这是内存测试最常排查的一类物理故障。
- **LFSR（线性反馈移位寄存器）**：用几位的异或反馈产生周期很长、看起来随机的位序列，是硬件里产生「伪随机」pattern 的标准手段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 定义模式常量 `C_MODE_*`、pattern 常量 `C_PATTERN_SEL_*`，以及字段宽度子类型 `RNG_MODE`、`RNG_PATTERN_SEL`。 |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心逻辑：状态机如何根据模式走不同分支、`ContRunning`/`ContIter` 如何实现 Continuous、`InitPattern`/`UpdatePattern` 如何按 pattern 种类生成数据。 |

## 4. 核心概念与源码讲解

### 4.1 运行模式：枚举常量与触发/停止语义

#### 4.1.1 概念说明

「模式」回答的是**这一次测试怎么跑**。本 IP 一共四种模式，定义在 package 里：

| 常量 | 值 | 语义 |
| --- | --- | --- |
| `C_MODE_SINGLE` | 0 | 写一遍 pattern → 读回比对一遍 → 回到 Idle。最常用的「跑一次看结果」。 |
| `C_MODE_CONTINUOUS` | 1 | 写→读→写→读……无限循环，直到写 STOP 寄存器。用于长时间压力测试。 |
| `C_MODE_WRITEONLY` | 2 | 只写 pattern，不读不比对。用于给存储器预置已知数据，或单独压测写通路。 |
| `C_MODE_READONLY` | 3 | 只读回比对，不写。要求存储器里**事先已有**相同 pattern（例如之前用 Single/WriteOnly 写过）。 |

模式写在 `REG_MODE`（地址 0x0C）的低 3 位（`RNG_MODE` 即 `2 downto 0`），先于 START 配置好。

#### 4.1.2 核心流程

一次测试的生命周期由 `mem_test.vhd` 里的主状态机 `Fsm_t` 驱动，模式决定它在哪些状态之间走：

```
START 寄存器置位
   │
   ├─ READONLY ────────────────────────► RdCmd_s ──► Read_s ──► (结束)
   └─ 其它 ──► WrCmd_s ──► Write_s
                                │
                                ├─ WRITEONLY ──► Idle_s  (写完即结束)
                                └─ 其它 ──────► RdCmd_s ──► Read_s
                                                        │
                                                        ├─ ContRunning=1 (CONTINUOUS) ──► WrCmd_s  (下一轮)
                                                        └─ ContRunning=0 ──► Idle_s
```

几个关键点：

- **Continuous 的进入**：在 START 时，若模式为 CONTINUOUS 就把内部标志 `ContRunning` 置 1，否则清 0。
- **Continuous 的退出**：写 STOP 寄存器只是把 `ContRunning` 清 0；当前这一轮的读操作**会跑完**，然后状态机看到 `ContRunning=0` 才回到 Idle。这是一种「优雅停止」，不是立即打断。
- **WRITEONLY / READONLY 的「跳过」**：WRITEONLY 在写完最后一拍后直接回 Idle（跳过读）；READONLY 在 START 时直接跳到 `RdCmd_s`（跳过写）。

#### 4.1.3 源码精读

模式常量定义在 package 里，字段宽度为 3 位（[hdl/mem_test_pkg.vhd:36-41](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L36-L41)）：

```vhdl
constant REG_MODE           : integer := 3;              -- 0x0C
subtype  RNG_MODE           is natural range 2 downto 0;
constant C_MODE_SINGLE      : integer := 0;
constant C_MODE_CONTINUOUS  : integer := 1;
constant C_MODE_WRITEONLY   : integer := 2;
constant C_MODE_READONLY    : integer := 3;
```

**Continuous 的进入与退出**在组合进程里（[hdl/mem_test.vhd:187-197](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L187-L197)）：

```vhdl
if RegStart_v = '1' then
    if RegMode_v = C_MODE_CONTINUOUS then
        v.ContRunning := '1';
    else
        v.ContRunning := '0';
    end if;
end if;
if RegStop_v = '1' then
    v.ContRunning := '0';
end if;
```

这段说明：`ContRunning` 在 START 时按模式决定，在 STOP 时无条件清零。

**START 时的状态分发**在 `Idle_s` 里（[hdl/mem_test.vhd:209-221](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L209-L221)）：

```vhdl
when Idle_s =>
    if RegStart_v = '1' then
        if RegMode_v = C_MODE_READONLY then
            v.Fsm := RdCmd_s;     -- READONLY：跳过写
        else
            v.Fsm := WrCmd_s;     -- 其它：先写
        end if;
        v.FirstErrAddr := (others => '0');
        v.Errors       := (others => '0');
        ...
```

注意：START 时 `Errors` 被清零。这一点对 Continuous 很重要——错误是**整个连续运行期间累计**的，不是每轮清零。

**WRITEONLY 写完即停**在 `Write_s` 里（[hdl/mem_test.vhd:241-247](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L241-L247)）：

```vhdl
if r.PatternCnt = r.CmdWr_Size-1 then       -- 写到最后一拍
    if RegMode_v = C_MODE_WRITEONLY then
        v.Fsm := Idle_s;                     -- WRITEONLY：直接结束
    else
        v.Fsm := RdCmd_s;                    -- 其它：转入读
    end if;
```

**Continuous 的循环回跳与迭代计数**在 `Read_s` 里（[hdl/mem_test.vhd:273-280](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L273-L280)）：

```vhdl
if r.PatternCnt = r.CmdRd_Size-1 then       -- 读到最后一拍
    v.ContIter := r.ContIter+1;             -- 完成一轮，迭代计数 +1
    if r.ContRunning = '1' then
        v.Fsm := WrCmd_s;                   -- CONTINUOUS：立刻开始下一轮
    else
        v.Fsm := Idle_s;                    -- 否则：结束
    end if;
```

这正是「优雅停止」的来源：STOP 只改 `ContRunning`，真正停在 Idle 发生在当前这轮读的最后一拍。

`ContIter` 通过 `REG_ITER`（0x34）对外可读，让你知道压力测试到底跑了多少轮；`ContRunning` 与 `ContIter` 都是 `two_process_r` 记录里的字段（[hdl/mem_test.vhd:94-111](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L94-L111)）。

#### 4.1.4 代码实践

**实践目标**：用源码确认「Continuous 模式下 STOP 之后到底发生什么」。

**操作步骤**：

1. 在 [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) 中定位 `Idle_s`、`Write_s`、`Read_s` 三个分支。
2. 假设当前正处于 `Read_s` 的中间（还没到最后一拍），此时软件写 STOP。
3. 跟踪 `RegStop_v`（L150）如何把 `ContRunning` 清 0（L195-197）。
4. 继续跟踪：状态机不会立即跳走，而是等到 `r.PatternCnt = r.CmdRd_Size-1`（L273）那一刻才检查 `ContRunning` 并回到 `Idle_s`。

**需要观察的现象**：STOP 写入后，`STATUS` 寄存器在当前这轮结束前仍会停留在 READING（值 2），直到本轮读完成才跳回 IDLE（值 0）。

**预期结果**：理解 STOP 是「本轮结束后停止」，而非「立即中止」。这一点在写上层轮询逻辑时很关键——你不能指望写完 STOP 立刻读到 IDLE。

#### 4.1.5 小练习与答案

**练习 1**：为什么 READONLY 模式要求存储器里「事先已有相同 pattern」？

> **答案**：READONLY 跳过写阶段，直接进入读+比对。比对时，IP 仍然**本地**按 `PATTERN_SEL` 重新生成参考 pattern（见 4.3 的 `InitPattern`），再和读回来的数据比。如果存储器里存的不是这个 pattern，几乎每一拍都会判为错误。所以 READONLY 通常接在一次 Single 或 WriteOnly（相同 pattern、相同地址范围）之后，用来「复读校验」。

**练习 2**：CONTINUOUS 模式下，`ERRORS` 寄存器反映的是「单轮错误数」还是「累计错误数」？

> **答案**：累计错误数。`Errors` 只在 START（`Idle_s`）时清零，之后每轮 `Read_s` 的比对错误都在原值上 +1（L289），不在轮间重置。配合 `ITER`（完成轮数）可以算出平均每轮错误率。

### 4.2 数据 pattern：枚举常量与生成规则

#### 4.2.1 概念说明

「pattern」回答的是**写进去的是什么数据**。本 IP 一共四种 pattern，写在 `REG_PATTERN_SEL`（地址 0x20）的低 3 位（`RNG_PATTERN_SEL`）：

| 常量 | 值 | 初始值 | 逐拍变化 | 善于暴露的故障 |
| --- | --- | --- | --- | --- |
| `C_PATTERN_SEL_COUNT` | 0 | 全 0 | 每拍 +1 | 地址译码错误、低位数据线 |
| `C_PATTERN_SEL_WALK1` | 1 | 只有 bit0=1 | 单个 1 逐位向高位移动并回绕 | **单根数据线 stuck-at（高/低）** |
| `C_PATTERN_SEL_OWNADD` | 2 | = 起始地址 | 每拍 +（数据宽度/8） | 地址线粘连、地址译码串扰 |
| `C_PATTERN_SEL_PRBN` | 3 | 低 16 位 = 0x6D3F | 16 位 LFSR 左移反馈 | 高翻转动密度，长时间压力/耦合故障 |

注意 pattern 的「宽度」就是 AXI 数据宽度 `AxiDataWidth_g`（默认 32）。四种 pattern 都在这个宽度上生成。

#### 4.2.2 核心流程

pattern 的生命周期分两步，由两个布尔标志触发：

```
进入 WrCmd_s / RdCmd_s
   └─► InitPattern = true   ──► 按当前 PATTERN_SEL 算出「第 0 拍」的 Pattern

每一拍握手成功（非最后一拍）
   └─► UpdatePattern = true ──► 按当前 PATTERN_SEL 由「上一拍」算出「下一拍」的 Pattern
```

- **初始化**在每次进入写命令态/读命令态时各做一次（写和读各自从第 0 拍重新开始，保证写进去的和读回来比对的参考序列完全一致）。
- **更新**在每一拍数据握手成功后做一次（最后一拍不再更新，因为马上要离开这个状态）。
- 四种 pattern 的具体公式见 4.3 源码精读。

一个关键直觉：**Walking-1 能同时抓 stuck-at-0 和 stuck-at-1**。因为任何一根线，在「这一拍的 1 落在它身上」时该是 1（stuck-at-0 会错），在其它所有拍该是 0（stuck-at-1 会错）。所以单根数据线粘连，Walking-1 几乎一定能暴露出来。

#### 4.2.3 源码精读

pattern 常量定义在 package（[hdl/mem_test_pkg.vhd:49-54](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L49-L54)）：

```vhdl
constant REG_PATTERN_SEL     : integer := 8;              -- 0x20
subtype  RNG_PATTERN_SEL     is natural range 2 downto 0;
constant C_PATTERN_SEL_COUNT : integer := 0;
constant C_PATTERN_SEL_WALK1 : integer := 1;
constant C_PATTERN_SEL_OWNADD: integer := 2;
constant C_PATTERN_SEL_PRBN  : integer := 3;
```

`Pattern`、`PatternCnt` 都是 `two_process_r` 记录里的字段（[hdl/mem_test.vhd:94-111](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L94-L111)）：`Pattern` 是当前拍要写/要比对的数据，`PatternCnt` 是当前 beat 计数，用于判断「最后一拍」以及回算字节地址。

模式的真正含义要结合状态机看——比如 READONLY 跳过写、WRITEONLY 跳过读、CONTINUOUS 循环，已经在 4.1 讲过；pattern 的具体生成公式则集中在 4.3。

#### 4.2.4 代码实践

**实践目标**：在源码里确认「写阶段和读阶段用的是同一套 pattern 序列」。

**操作步骤**：

1. 打开 [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd)。
2. 在 `WrCmd_s`（L224-234）找到 `v.PatternCnt := (others => '0'); InitPattern_v := true;`。
3. 在 `RdCmd_s`（L256-266）找到**同样**的两句。
4. 结论：写和读各自独立地把 pattern 重置到第 0 拍并重新初始化，因此只要 `PATTERN_SEL`、`ADDR`、`SIZE` 不变，写进去的第 N 拍和读回来比对的第 N 拍参考值必然相等。

**预期结果**：理解为什么只要硬件正常，`ERRORS` 一定是 0——参考序列和写入序列是同一段确定性计算。

#### 4.2.5 小练习与答案

**练习 1**：如果误把 `REG_PATTERN_SEL` 写成一个不存在的值（例如 5），会发生什么？

> **答案**：4.3 会看到 `InitPattern`/`UpdatePattern` 的 `case` 里有一个 `when others => v.Fsm := IntError_s;` 分支。pattern 种类不在 0..3 范围内时，状态机直接进入不可恢复的内部错误态 `IntError_s`，`STATUS` 反映为 `C_STATUS_INTERR`（值 6）。所以错误的 pattern 值不会静默跑出错数据，而是被显式拦截。

**练习 2**：Walking-1 在一个 32 位数据总线上要多少拍才能让「1」走完所有位置？

> **答案**：32 拍（数据宽度 `AxiDataWidth_g` 拍）。第 0 拍 1 在 bit0，第 31 拍 1 在 bit31，第 32 拍回绕回 bit0。所以用 Walking-1 时，测试范围 `SIZE` 至少要覆盖 32 个 beat（即 32 × 4 = 128 字节，按 32 位宽算）才有意义，否则 1 走不完一轮。

### 4.3 pattern 初始化与更新逻辑

#### 4.3.1 概念说明

这一节把四种 pattern 的**生成算法**逐条讲清楚。核心是 package 里读到的两个标志：

- `InitPattern_v`：进入命令态时为真，算「第 0 拍」。
- `UpdatePattern_v`：每拍握手后为真，算「下一拍」。

两者都是 `p_comb` 进程里的局部 `boolean` 变量（[hdl/mem_test.vhd:138-139](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L138-L139)），在状态机的 `case` 里被置位，然后在进程末尾的两段「共享代码」里统一处理。

#### 4.3.2 核心流程

四种 pattern 的数学表达（记 `D = AxiDataWidth_g`，第 n 拍 pattern 为 \(P_n\)，地址递增步长为 \(D/8\) 字节，起始地址为 \(A_0\)）：

**Counter**

\[ P_0 = 0,\qquad P_{n+1} = P_n + 1 \]

简单的递增计数，覆盖各种低位组合。

**Walking-1**

\[ P_0 = 1,\qquad P_{n+1} = \text{rol}_1(P_n) \]

其中 \(\text{rol}_1\) 是循环左移 1 位（最高位回绕到最低位）。单个 1 从 bit0 逐拍走向高位，到顶后回绕。

**OwnAddress**

\[ P_0 = A_0,\qquad P_{n+1} = P_n + (D/8) \]

即「这一拍的数据等于这一拍的字节地址」。地址每拍前进一个字（\(D/8\) 字节），数据同步前进。

**PseudoRandom（LFSR）**

16 位 Fibonacci LFSR，反馈抽头为第 15、13、12、10 位的异或，整体左移、新位填入 bit0：

\[ P_0 = \texttt{0x6D3F}\ (\text{低 16 位}),\qquad P_{n+1,\,0} = P_{n,15}\oplus P_{n,13}\oplus P_{n,12}\oplus P_{n,10},\quad P_{n+1,\,i}=P_{n,\,i-1}\ (i\ge 1) \]

抽头组合 {16,14,13,11}（即 0 基的 {15,13,12,10}）是 16 位 LFSR 的极大长度抽头，周期为 \(2^{16}-1\)，序列近似随机、翻转动密度高。

#### 4.3.3 源码精读

**初始化**（[hdl/mem_test.vhd:312-327](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L312-L327)）：

```vhdl
if InitPattern_v then
    case to_integer(RegPatternSel_v) is
        when C_PATTERN_SEL_COUNT  => v.Pattern := (others => '0');
        when C_PATTERN_SEL_WALK1  => v.Pattern := (others => '0'); v.Pattern(0) := '1';
        when C_PATTERN_SEL_OWNADD => v.Pattern := std_logic_vector(resize(RegAddr_v, v.Pattern'length));
        when C_PATTERN_SEL_PRBN   => v.Pattern := (others => '0'); v.Pattern(15 downto 0) := X"6D3F";
        when others               => v.Fsm := IntError_s;
    end case;
end if;
```

要点：

- COUNT / WALK1 / PRBN 的高位都先清零，PRBN 只在低 16 位种入 `0x6D3F`。
- OWNADD 把 64 位起始地址 `RegAddr_v` 截断/扩展到数据宽度——所以 OwnAddress pattern 的第 0 拍就是基地址本身。
- `when others` 兜底：pattern 值非法 → `IntError_s`（见 4.2.5 练习 1）。

**更新**（[hdl/mem_test.vhd:330-345](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L330-L345)）：

```vhdl
if UpdatePattern_v then
    case to_integer(RegPatternSel_v) is
        when C_PATTERN_SEL_COUNT  => v.Pattern := std_logic_vector(unsigned(r.Pattern) + 1);
        when C_PATTERN_SEL_WALK1  => v.Pattern(0) := r.Pattern(r.Pattern'high);
                                     v.Pattern(v.Pattern'high downto 1) := r.Pattern(r.Pattern'high-1 downto 0);
        when C_PATTERN_SEL_OWNADD => v.Pattern := std_logic_vector(unsigned(r.Pattern) + AxiDataWidth_g/8);
        when C_PATTERN_SEL_PRBN   => v.Pattern(0) := r.Pattern(15) xor r.Pattern(13) xor r.Pattern(12) xor r.Pattern(10);
                                     v.Pattern(v.Pattern'high downto 1) := r.Pattern(r.Pattern'high-1 downto 0);
        when others               => v.Fsm := IntError_s;
    end case;
end if;
```

要点：

- **Walking-1 的「循环左移」**：`new[0] = old[high]`，`new[i] = old[i-1]`（i≥1）。即每个位左移一位、最高位回绕到 bit0，单 1 由此从低位走向高位再回绕。
- **PRBN 同样是「左移 + 反馈」**：高位整体左移（`new[i]=old[i-1]`），新 bit0 由抽头异或得到。这是标准 Fibonacci LFSR 写法。
- **OWNADD 每拍 +`AxiDataWidth_g/8`**：正好是一个字的字节数，所以数据严格跟随字节地址。

读回比对在 `Read_s` 里（[hdl/mem_test.vhd:287-295](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L287-L295)）：当 `RdDat_Data /= r.Pattern` 时，`Errors` 自增并在首次错误时记录 `FirstErrAddr`。比对用的 `r.Pattern` 就是由上面这套 Init/Update 生成的参考序列——这就是 pattern 的「意义」所在。字节地址如何由 beat 计数回算留给 [u3-l4](u3-l4-pattern-generation-and-check.md)。

#### 4.3.4 代码实践

**实践目标**：手推 PseudoRandom pattern 的前几拍，验证对 LFSR 更新逻辑的理解（本练习为「源码阅读型手算」，结果待本地仿真复核）。

**操作步骤**：

1. 取 16 位视图（数据宽度 ≥16 时，低 16 位的 LFSR 序列与宽度无关）。种子 \(P_0 = \texttt{0x6D3F}\)。
2. 按更新公式逐拍计算：`new = (old 左移 1 位，丢掉原最高位) | (b15 xor b13 xor b12 xor b10)`。
3. 填表（仅看低 16 位）：

| 拍 n | \(P_n\)（16 进制） | b15 | b13 | b12 | b10 | 反馈位 |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | `0x6D3F` | 0 | 1 | 0 | 1 | 0 |
| 1 | `0xDA7E` | 1 | 0 | 1 | 0 | 0 |
| 2 | `0xB4FC` | 1 | 1 | 1 | 1 | 0 |
| 3 | `0x69F8` | 0 | 1 | 0 | 0 | 1 |
| 4 | `0xD3F1` | — | — | — | — | — |

推算示例（拍 0 → 拍 1）：

- `0x6D3F` 左移 1 位 = `0xDA7E`（最高位原为 0，无溢出），反馈位 = 0 xor 1 xor 0 xor 1 = 0，所以 \(P_1 = \texttt{0xDA7E}\)。
- `0xDA7E` 左移 1 位 = `0x1B4FC`，截到 16 位 = `0xB4FC`（丢掉溢出的最高位 1），反馈 = 1 xor 0 xor 1 xor 0 = 0，所以 \(P_2 = \texttt{0xB4FC}\)。
- 依此类推得到 \(P_3 = \texttt{0x69F8}\)、\(P_4 = \texttt{0xD3F1}\)。

**需要观察的现象 / 预期结果**：低 16 位每拍整体左移、bit0 填入异或结果，序列看起来无规律。上述手算结果**待本地验证**：可在 [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd) 里用 PRBN pattern 跑一轮仿真，抓 `mem_test` 内部 `r.Pattern` 信号对比。

#### 4.3.5 小练习与答案

**练习 1**：Counter pattern 写入 32 位宽、起始地址 0、SIZE = 64 字节（16 拍）。写出前 4 拍和最后 1 拍的 pattern 值。

> **答案**：第 0 拍 = `0x00000000`，第 1 拍 = `0x00000001`，第 2 拍 = `0x00000002`，第 3 拍 = `0x00000003`；最后第 15 拍 = `0x0000000F`。可见 Counter 只在低 4 位变化，对高位数据线几乎无覆盖——这正是它不适合做全面数据线诊断的原因。

**练习 2**：OwnAddress pattern 在 32 位宽下，起始地址 `0x10000000`、SIZE = 16 字节（4 拍）。写出 4 拍的值，并说明它为什么适合查地址线故障。

> **答案**：步长 = 32/8 = 4 字节。4 拍分别为 `0x10000000`、`0x10000004`、`0x10000008`、`0x1000000C`。每一拍的数据等于该拍的真实字节地址，所以一旦某根地址线粘连（导致实际访问的地址和预期不一致），读回来的数据会和本地按地址生成的参考值不符，立刻暴露。这就是 OwnAddress 的设计动机。

## 5. 综合实践

针对下面两个真实调试需求，选出**合适的 pattern + mode 组合**，并结合本讲源码说明理由。

**需求 A：怀疑某条数据线粘连（stuck-at）**

- **推荐组合**：`C_PATTERN_SEL_WALK1`（Walking-1）+ `C_MODE_SINGLE`（Single）。
- **理由**：
  - pattern 选 Walking-1——单 1 逐位走遍所有数据位。对任一根线：1 落在它身上时该读 1（stuck-at-0 会错），其它拍该读 0（stuck-at-1 会错）。所以无论粘高还是粘低，Walking-1 都能把出错的拍暴露成 `ERRORS`，并通过 `FIRSTERR` 给出线索（详见 [u3-l4](u3-l4-pattern-generation-and-check.md)）。
  - mode 选 Single——一次写一遍、读一遍即可定位，结果干净（`ERRORS` 就是这一遍的出错拍数）。Continuous 会反复累加反而干扰判断；READONLY/WRITEONLY 会跳过一半通路，不适合首次诊断。
- **操作要点**：设置 `PATTERN_SEL=1`、`MODE=0`、地址对齐到数据宽度、`SIZE` 至少覆盖 `AxiDataWidth_g` 个 beat（让 1 走完一轮），写 START，轮询 `STATUS` 回 IDLE 后读 `ERRORS`/`FIRSTERR`。

**需求 B：想长时间压力测试 DDR**

- **推荐组合**：`C_PATTERN_SEL_PRBN`（PseudoRandom）+ `C_MODE_CONTINUOUS`（Continuous）。
- **理由**：
  - pattern 选 PRBN——16 位极大长度 LFSR，翻转动密度高、近似随机，能同时压数据线、地址线、内部刷新与耦合效应，是长时间压力测试的首选。Counter/Walk1 覆盖模式太规整，难以暴露耦合与时序类间歇故障。
  - mode 选 Continuous——写一次 START 后无人值守地写→读→写→读，`ITER` 累计完成轮数，`ERRORS` 累计整个运行期间的错误，非常适合跑几小时甚至几天。结束时写 STOP（优雅停止：当前这轮跑完才回 Idle），再读 `ITER`、`ERRORS` 评估可靠性。
- **操作要点**：设置 `PATTERN_SEL=3`、`MODE=1`、覆盖较大地址范围，写 START；定期读 `ITER`/`ERRORS` 监控；测试结束写 `STOP` 并等待 `STATUS` 回 IDLE。

把这两个组合各写一小段 C 驱动伪代码（参考 [u2-l3](u2-l3-c-driver.md) 的 API），比较两次调用在 `MODE`/`PATTERN_SEL` 寄存器上的差异。

## 6. 本讲小结

- 四种**模式**由 `C_MODE_*`（0..3）选择，决定状态机走哪些分支：SINGLE 跑一遍、CONTINUOUS 循环到 STOP、WRITEONLY 只写、READONLY 只读。
- **Continuous 的关键机制**是 `ContRunning` 标志（START 时按模式置位、STOP 时清零）和 `ContIter` 迭代计数；STOP 是「优雅停止」，当前这轮读完后才回 Idle。
- 四种 **pattern** 由 `C_PATTERN_SEL_*`（0..3）选择，决定写什么数据：Counter 递增、Walking-1 单 1 循环左移、OwnAddress 数据=地址、PRBN 16 位 LFSR 左移反馈。
- pattern 生成统一由 `InitPattern_v`（进入命令态）和 `UpdatePattern_v`（每拍握手）两个标志触发；写阶段和读阶段用**同一套**确定性序列，因此硬件正常时 `ERRORS` 必为 0。
- pattern 值非法会被 `when others => IntError_s` 显式拦截，不会静默跑错。
- 选型直觉：查数据线粘连用 Walking-1，查地址线用 OwnAddress，长时间压力用 PRBN+Continuous。

## 7. 下一步学习建议

- 下一讲 [u2-l3 C 软件驱动](u2-l3-c-driver.md) 会把这些 `C_MODE_*` / `C_PATTERN_SEL_*` 常量和 C 头文件里的宏一一对应，教你用 API 跑一次完整测试。
- 想深入 pattern 读回比对、首个错误地址的字节换算，以及 LFSR 在仿真里的精确序列，请读 [u3-l4 Pattern 生成、数据校验与首个错误地址](u3-l4-pattern-generation-and-check.md)。
- 想理解模式/pattern 配置如何经由 AXI-Lite 写入寄存器，请读 [u4-l1 AXI-Lite 从机与寄存器译码](u4-l1-axi-lite-slave.md)。
