# CRC 引擎与包校验

## 1. 本讲目标

本讲讲解 Open Logic 如何用一套 CRC（Cyclic Redundancy Check，循环冗余校验）组件为 AXI4-Stream 包流提供端到端的完整性保护。学完后你应该能够：

- 说清楚 CRC 的数学本质，以及用 LFSR（线性反馈移位寄存器）逐位计算 CRC 的原理。
- 读懂 `olo_base_crc` 计算引擎的接口、泛型与逐位 LFSR 实现。
- 理解 `olo_base_crc_append` 如何在写侧（TX）把 CRC 作为额外一个数据拍追加到包尾。
- 理解 `olo_base_crc_check` 如何在读侧（RX）复算 CRC、把复算结果与收到的 CRC 比较，并在 `DROP`/`FLAG` 两种模式下处理校验失败的包。
- 把 append 与 check 串接成一条「发—传—收」的保护链路，并能用仿真注入错误、验证错误包被正确丢弃。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**CRC 是什么。** CRC 是一种「检错码」。发送方把一段数据当作一个很长的一进制数，用它除以一个约定好的「生成多项式」，得到的「余数」就是 CRC。发送方把数据连同这个余数一起发出去；接收方用同样的多项式再除一次，如果余数为零（或等于一个约定值），就认为数据在传输中没有出错。CRC 的强大之处在于：它对常见的「突发错误」（连续若干位同时翻转）有很强的检出能力，而且用很简单的硬件（一个移位寄存器）就能实时算出来。

**GF(2) 多项式除法。** CRC 的除法不是十进制除法，而是在 GF(2)（只有 0、1 两个元素）上的多项式除法，其加减法都等同于「异或 XOR」。把数据的每一位看作多项式的一项系数：

\[ M(x) = m_{k-1}x^{k-1} + \dots + m_1 x + m_0 \]

生成多项式 \( G(x) \) 的次数为 \( r \)（即 CRC 的位宽）。先在数据后补 \( r \) 个 0（相当于乘以 \( x^r \)），再对 \( G(x) \) 取模，余数 \( R(x) \) 就是 CRC：

\[ R(x) = \big( M(x) \cdot x^{r} \big) \bmod G(x) \]

因为加减都是 XOR，整个除法过程可以用「移位 + 条件 XOR」实现，这正是 LFSR 的工作方式。

**LFSR（线性反馈移位寄存器）。** 一个 \( r \) 位的寄存器，每来一个输入位：把输入位与寄存器最高位（即将被移出去的那一位）异或得到反馈位，寄存器左移一位，若反馈位为 1 再把生成多项式异或回去。处理完整段数据后，寄存器里的内容就是 CRC。这是硬件实现 CRC 的标准做法，资源极省。

**AXI4-Stream 与包（packet）。** Open Logic 的数据通路普遍采用 AXI4-Stream（简称 AXI-S）握手：`Valid` 表示数据有效、`Ready` 表示下游准备好接收，二者同时为高才完成一次传输（一个「beat / 拍」）。`Last` 信号标记一个包的最后一拍。本讲的 append/check 都工作在「包」粒度上：CRC 针对整个包计算一次。

**字节使能（Byte Enable）的 Trailing-Only 约定。** 当数据位宽是 8 的整数倍时，`olo_base_crc` 支持 `In_Be`（字节使能）。Open Logic 全库统一遵守「Trailing-Only」约定：只有在最后一拍（`In_Last='1'`）才允许屏蔽字节，且被使能的字节必须从最低位起连续、无空洞（详见 `doc/Conventions.md`）。

> 本讲承接 [u2-l2 流水线阶段与 AXI-S 握手]：append/check 内部都复用了 `olo_base_pl_stage` 来切断 `Out_Ready → In_Ready` 的组合路径，并把两进程法（two-process method）作为标准写法。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `src/base/vhdl/olo_base_crc.vhd` | CRC 计算引擎。基于 LFSR，每拍吃一个数据字、逐位更新；在 `In_Last` 时输出最终 CRC。是另外两个实体的公共依赖。 |
| `src/base/vhdl/olo_base_crc_append.vhd` | 写侧（TX）。在数据包末尾追加一拍，把 `olo_base_crc` 算出的 CRC 放在该拍的低位。 |
| `src/base/vhdl/olo_base_crc_check.vhd` | 读侧（RX）。复算 CRC，与收到的 CRC 比较；`DROP` 模式用包 FIFO 丢弃坏包，`FLAG` 模式只标记不丢弃。 |
| `test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd` | 把 append 与 check 串成一条链路、并带错误注入（XOR 翻转）的 VUnit 测试台，是本讲综合实践的样板。 |
| `doc/base/olo_base_crc.md` | 引擎的接口、泛型与标准 CRC 参数表（对应 crccalc.com）。 |

## 4. 核心概念与源码讲解

### 4.1 CRC 计算引擎（olo_base_crc）

#### 4.1.1 概念说明

`olo_base_crc` 是一块「裸」的 CRC 计算器：它不关心包怎么发、CRC 怎么用，只负责把流入的数据逐位喂进 LFSR，并在收到 `In_Last`（包尾）时给出这个包的 CRC。它既能用在发送侧（算出 CRC 发出去），也能用在接收侧（算出 CRC 去和收到的比），所以是 append 与 check 共同复用的地基。

它的「高度可配置」是为了能用同一份代码复现几乎所有工业标准 CRC（CRC-8/DVB-S2、CRC-16/DECT、CRC-32 等）。Open Logic 以 [crccalc.com](https://crccalc.com) 的记法为参照：一个标准 CRC 由「多项式、初值、位序、字节序、输出位翻转、输出 XOR 掩码」六个参数完全决定，它们直接对应引擎的六个泛型。

#### 4.1.2 核心流程

引擎的核心是一个单进程 `p_lfsr`，每个时钟上升沿完成：

1. **字节使能处理**：若处于最后一拍且数据是字节对齐的，按 `In_Be` 计算有效数据的高位边界，只保留有效字节（无效高位字节视为不存在）。
2. **位序/字节序重排**：把输入数据按 `BitOrder_g` / `ByteOrder_g` 重排成「LFSR 实际处理的位序」。LFSR 内部恒按 MSB 先行，所有协议差异都靠这一步的排列吸收。
3. **逐位 LFSR 更新**：对重排后的每一位执行标准反馈——反馈位 = 输入位 XOR 当前最高位；寄存器左移；反馈位为 1 则异或多项式。
4. **包尾处理**：`In_Last='1'` 时，把当前 LFSR 内容（经可选位翻转与 XOR 掩码）送到 `Out_Crc`，拉高 `Out_Valid`，并把 LFSR 复位到初值，为下一个包做准备。
5. **`In_First` 复位**：若上一个包被「放弃」（没走到 `In_Last`），可在下一个包第一拍用 `In_First='1'` 强制把 LFSR 重新装载初值，避免脏状态污染新包。

逐位 LFSR 的单步运算（MSB 先行）可写成：

\[ \text{fb} = d_i \oplus R_{r-1}, \qquad R \leftarrow (R \ll 1), \qquad \text{if fb}=1:\ R \leftarrow R \oplus G \]

其中 \( d_i \) 是当前输入位，\( R \) 是 \( r \) 位 LFSR，\( G \) 是生成多项式，\( \oplus \) 为异或。

#### 4.1.3 源码精读

**泛型与端口。** 泛型就是上面提到的六个 CRC 参数；端口是标准的 AXI-S 输入（带 `In_Last`/`In_First`/`In_Be`）和一个 CRC 输出：

- 泛型定义与含义见 [src/base/vhdl/olo_base_crc.vhd:34-43](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L34-L43)。注意 `Polynomial_g` 既给出多项式、其位宽又同时决定了 CRC 的位宽。
- 端口定义见 [src/base/vhdl/olo_base_crc.vhd:44-59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L44-L59)。所有可选输入都有默认值（`In_Valid` 默认 `'1'`、`In_Be` 默认全 `'1'`），不用的功能可直接悬空。

**关键常量。** 初值与 XOR 掩码都被「尺寸规整」到 CRC 位宽，并用 `choose()` 在「用户填了 `"0"`」与「用户填了真实值」之间选择，注释里写明这是为了规避 ModelSim 对 `choose()` 两侧位宽不一致的编译限制：

[src/base/vhdl/olo_base_crc.vhd:73-78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L73-L78) —— 把 `InitialValue_g` / `XorOutput_g` 规整并选择。

**逐位 LFSR 循环（全引擎最核心的几行）。** 循环范围必须是静态的（变量范围不可综合），所以遍历 `In_Data` 的全部位，再用 `if bit <= InputHigh_v` 跳过被字节使能屏蔽的高位：

[src/base/vhdl/olo_base_crc.vhd:183-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L183-L199)。这段就是上一节公式的直接翻译：

```vhdl
-- 反馈位 = 输入位 XOR 当前最高位
InBit_v := Input_v(bit) xor Lfsr_v(Lfsr_v'high);
-- 寄存器左移一位，低位补 0
Lfsr_v := Lfsr_v(Lfsr_v'high-1 downto 0) & '0';
-- 反馈位为 1 时，异或多项式
if InBit_v = '1' then
    Lfsr_v := Lfsr_v xor Polynomial_g;
end if;
```

**输出与包尾。** 先做可选位翻转（`BitflipOutput_g`），再异或输出掩码（`XorOutput_g`，且在位翻转之后施加）；`In_Last` 时拉高 `Out_Valid_I` 并把 LFSR 复位到初值：

[src/base/vhdl/olo_base_crc.vhd:201-213](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L201-L213) —— 生成最终 CRC、处理包尾。

**反压（输入允许条件）。** `In_Ready` 是纯组合信号，仅当「上一包的 CRC 结果还没被下游读走」时才拉低，避免新数据把尚未消费的 CRC 冲掉：

[src/base/vhdl/olo_base_crc.vhd:227](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L227) —— `In_Ready_I <= Out_Ready or not Out_Valid_I;`

**一个值得记住的时序特性。** 文档强调：虽然 `Out_Valid` 只在 `In_Last` 后才拉高，但 `Out_Crc` 在**每个**输入字之后一拍就更新一次。这一点被 `olo_base_crc_check` 巧妙利用（见 4.3）。

#### 4.1.4 代码实践（源码阅读 + 跑测试）

**目标**：手动追踪一个字流过 LFSR，再用仿真确认引擎行为。

1. **阅读型追踪**。打开 [src/base/vhdl/olo_base_crc.vhd:183-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L183-L199)。假设配置为 CRC-8/DVB-S2（`Polynomial_g = x"D5"`，初值 `0x00`），输入一个数据字 `0x00`：因为每一位都是 0，反馈位恒为 `0 XOR 最高位`，但因为初值也是 0，最高位始终为 0，所以 LFSR 全程不变，最终 CRC 也为 `0x00`。再换一个非零输入，按上述三行手算 8 步，记录每步的 LFSR 值。
2. **运行仿真**。在 `sim/` 目录运行引擎的参数化测试台（VUnit 按子串匹配测试名）：
   ```bash
   cd sim
   python run.py --ghdl olo_base_crc_tb
   ```
3. **观察现象**。该测试台覆盖了多种 `CrcWidth × DataWidth` 组合、`BitOrder/ByteOrder` 全排列、`BitflipOutput/XorOutput` 开关（见 `sim/test_configs/olo_base.py` 中 `olo_base_crc_tb` 一段）。
4. **预期结果**。所有用例通过（pass）。它对照的金标准是 `test/base/olo_base_crc/CrcCalculator.ods`（一份 LibreOffice 电子表格，内含参考 CRC 值）。
5. **待本地验证**：具体的用例数量与命令行精确输出取决于本地环境，运行后以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Polynomial_g` 既能表示多项式、又决定了 CRC 位宽？
> **答案**：引擎取 `Polynomial_g'length` 作为 `CrcWidth_c`（见 [src/base/vhdl/olo_base_crc.vhd:70](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L70)）。生成多项式的次数等于 CRC 余数的位宽，所以多项式向量的位数天然就是 CRC 宽度。

**练习 2**：`In_First` 与 `In_Last` 在「复位 LFSR 到初值」这件事上有什么区别？
> **答案**：`In_Last` 标志正常包尾，在输出 CRC 的同一拍把 LFSR 复位到初值（[L209-L213](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L209-L213)）；`In_First` 用于「异常中止」的包——上一个包没走到 `In_Last` 就被放弃，下一个包第一拍用 `In_First` 把 LFSR 重新装载初值，避免脏状态影响新包（[L175-L179](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L175-L179)）。

### 4.2 写侧追加 CRC（olo_base_crc_append）

#### 4.2.1 概念说明

`olo_base_crc_append` 解决的是发送侧的需求：在用户给出的数据包后面，自动追加一拍，把该包的 CRC 放进去。它对用户完全透明——用户只管按普通 AXI-S 包喂数据并打 `In_Last`，输出端会多出一拍「CRC 拍」，且这一拍的 `Out_Last='1'`。

约束：CRC 位宽必须 ≤ 数据位宽。若 CRC 较窄，它被放在数据字的**低位**，高位补零（见 `doc/base/olo_base_crc_append.md`）。这一约定与读侧 `olo_base_crc_check` 的「比较低位」严格对应。

#### 4.2.2 核心流程

实体内部用一个两态状态机 + 一个 `olo_base_crc` 实例 + 一个 `olo_base_pl_stage`：

1. **`Data_s`（传数据态）**：把输入数据原样接进内部的 `olo_base_crc`（用 `In_Beat` 标记真正完成握手的拍，喂给引擎），同时把数据送向输出。当看到 `In_Last` 且当前拍握手成功，转入 `Crc_s`。
2. **`Crc_s`（发 CRC 态）**：此时引擎已在上一拍算完整个包的 CRC（引擎输出比输入晚一拍，正好在包尾后一拍就绪）。把 `Crc_Crc` 放到输出数据的低位、`Out_Last='1'`，等下游收走后回到 `Data_s`。
3. **输出寄存**：数据与 `Last` 拼成 `Width_g+1` 位，过一个 `olo_base_pl_stage`，目的是切断 `Out_Ready → In_Ready` 的纯组合路径（见 u2-l2）。

时序上的两个特征（见文档波形说明）：输出流无气泡；输入流每包有恰好一个停顿周期（即追加 CRC 那拍不收新数据）。

#### 4.2.3 源码精读

**状态机类型**：只有两个状态，用 record 收纳（两进程法）：

[src/base/vhdl/olo_base_crc_append.vhd:70-77](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L70-L77) —— `State_t := (Data_s, Crc_s)`，状态收进 `TwoProcess_r`。

**组合进程里的 FSM**（关键部分）：

[src/base/vhdl/olo_base_crc_append.vhd:111-136](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L111-L136)。要点：

```vhdl
when Data_s =>
    Pl_Data  <= In_Data;          -- 数据原样向下传
    Pl_Valid <= In_Valid;
    In_Ready <= Pl_Ready;
    In_Beat  <= In_Valid and Pl_Ready;   -- 真正握手的那拍才喂 CRC 引擎
    if In_Valid='1' and Pl_Ready='1' and In_Last='1' then
        v.State := Crc_s;          -- 包尾，转入发 CRC
    end if;

when Crc_s =>
    Pl_Data(Crc_Crc'range) <= Crc_Crc;  -- CRC 放低位
    Pl_Last  <= '1';                     -- CRC 拍即输出包尾
    Crc_Ready <= Pl_Ready;              -- 反压传给 CRC 引擎的输出
```

注意 `In_Beat`：只有真正完成握手的数据拍才被计入 CRC，反压期间的数据不会重复计入，保证正确性。

**CRC 引擎实例化**：把六个 CRC 参数透传，输入有效用 `In_Beat`（而非 `In_Valid`），`In_Last` 直接透传：

[src/base/vhdl/olo_base_crc_append.vhd:157-176](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L157-L176)。

**输出寄存器（pl_stage 切断组合路径）**：把 `Pl_Last` 与 `Pl_Data` 拼接成 `DataWidth_g+1` 位过一级 `olo_base_pl_stage`，再拆回 `Out_Last` 与 `Out_Data`：

[src/base/vhdl/olo_base_crc_append.vhd:178-207](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L178-L207)。文档明确指出这一级是为避免 `Out_Ready → In_Ready` 的直接组合路径。

#### 4.2.4 代码实践（源码阅读型）

**目标**：在源码里定位「每包一个停顿周期」是怎么产生的。

1. 阅读 [src/base/vhdl/olo_base_crc_append.vhd:104-116](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L104-L116)。注意组合进程开头把 `In_Ready <= '0'` 作为默认值。
2. 进入 `Crc_s` 分支 [L122-L131](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L122-L131)，确认：在发 CRC 那拍，`In_Ready` 没有被重新置 1（仍是默认 `'0'`），所以上游必须停一拍。
3. **观察现象（推理）**：连续发一个 4 拍的包，输出端会出现 4 拍数据 + 1 拍 CRC = 5 拍，且这 5 拍之间无气泡；输入端在发完第 4 拍（`In_Last`）后必须空闲 1 拍，第 5 拍不能收新数据。
4. **预期结果**：与 `doc/base/olo_base_crc_append.md` 波形说明一致——「输出无气泡、输入每包一拍停顿」。
5. 想直接看波形可运行 `cd sim && python run.py --ghdl olo_base_crc_append_tb`，在 GHDL/GTKWave 中观察 `In_Ready` 在包尾后拉低一拍（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么喂给内部 CRC 引擎的是 `In_Beat`（`= In_Valid and Pl_Ready`）而不是 `In_Valid`？
> **答案**：只有当下游真正接收（`Pl_Ready='1'`）且数据有效时，这一拍才「真正被消费」，CRC 才应计入这拍数据。若直接用 `In_Valid`，在反压期间（`Pl_Ready='0'` 但 `In_Valid='1'`）同一拍会被重复计入，算出错误 CRC。

**练习 2**：如果 `CrcPolynomial_g` 比 `DataWidth_g` 还宽会怎样？
> **答案**：实体在 [L92-L94](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_append.vhd#L92-L94) 有断言拦截，报错 `Polynomial_g must be smaller or equal width than DataWidth_g`，因为 CRC 拍要装进一个数据字里放不下。

### 4.3 读侧校验与丢弃（olo_base_crc_check）

#### 4.3.1 概念说明

`olo_base_crc_check` 是接收侧的对应物：它复算流入数据的 CRC，并与包尾那一拍里收到的 CRC 比较。它有两种工作模式（`Mode_g`）：

- **`DROP`（默认）**：校验失败的整包被丢弃，不出现在输出上；`Out_CrcErr` 拉高一个周期作脉冲指示。因为要等整包收完、比完 CRC 才能决定丢不丢，内部必须缓存整包——用的是 `olo_base_fifo_packet`（包 FIFO，`DROP_ONLY` 子集）。
- **`FLAG`**：所有包都照常输出，但坏包的最后一拍用 `Out_CrcErr='1'` 标记。无需缓存整包，只需一级 `olo_base_pl_stage`，故延迟更低。

关键规则：**CRC 只对除最后一拍以外的所有数据计算**——最后一拍装的就是 CRC 本身，当然不能把它也算进去。下面会看到，引擎「输出比输入晚一拍」的特性恰好让这条规则在硬件里自然成立。

#### 4.3.2 核心流程

实体用一个两态 FSM 配合一个 `olo_base_crc` 实例：

1. **`First_s`（等首拍）**：每个包的第一拍只接收、送入 CRC 引擎并缓存，**不**转发到输出（因为它要等到整包比完 CRC 才决定去留）。收到非 `In_Last` 的首拍后转入 `Others_s`。
2. **`Others_s`（后续拍）**：输入与输出缓冲（FIFO 或 pl_stage）之间直接握手；同时每一拍都送 CRC 引擎。当 `In_Last` 到来时，比较 `In_Data` 低位（收到的 CRC）与 `Crc_Crc`（引擎复算的 CRC），不等则置 `Pl_CrcErr`。
3. **DROP 分支**：把 `Pl_CrcErr` 接到包 FIFO 的 `In_Drop`——坏包在写完瞬间被丢弃。
4. **FLAG 分支**：把 `Pl_CrcErr` 当作一个数据标志位，与数据、`Last` 拼接送过 pl_stage，在包尾拍输出。

#### 4.3.3 源码精读

**「一拍延迟」如何让 CRC 排除最后一拍**（本实体最精妙之处）。看 `Others_s` 里处理 `In_Last` 的比较：

[src/base/vhdl/olo_base_crc_check.vhd:125-139](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L125-L139)：

```vhdl
when Others_s =>
    In_Ready <= Pl_Ready;
    Pl_Valid <= In_Valid;
    if Pl_Ready='1' and In_Valid='1' then
        v.Data := In_Data;
        if In_Last = '1' then
            v.Fsm := First_s;
            if In_Data(Crc_Crc'range) /= Crc_Crc then  -- 比较收到的 CRC 与复算的 CRC
                Pl_CrcErr <= '1';
            end if;
        end if;
    end if;
```

这里 `Crc_Crc` 是 `olo_base_crc` 的 `Out_Crc`。由于引擎输出比输入晚一拍，当 `In_Last` 拍（即 CRC 拍）到达比较逻辑的这一刻，`Crc_Crc` 反映的是**前一拍**（也就是真正的最后一拍数据）处理完后的 CRC——即「所有数据拍、不含 CRC 拍」的 CRC。于是 `In_Data(Crc_Crc'range) /= Crc_Crc` 正好比较「收到的 CRC」与「据所有数据复算的 CRC」。虽然这一拍的数据也被送进了引擎（`In_Beat`），但它产生的结果要到下一拍才出现，本拍用不到，因此不影响比较。文档里「CRC is calculated over all data words except the last one」正是由这个时序差自然实现。

**首拍缓存、不转发**：

[src/base/vhdl/olo_base_crc_check.vhd:115-123](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L115-L123) —— `First_s` 中 `In_Ready <= '1'`、`Pl_Valid <= '0'`，首拍只入不出；同时忽略「只有 CRC、没有数据」的单拍包。

**DROP 模式：包 FIFO 丢坏包**。`Pl_CrcErr` 直接接 `In_Drop`，FIFO 用 `DROP_ONLY` 子集（最省资源、不限最大包数）：

[src/base/vhdl/olo_base_crc_check.vhd:188-214](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L188-L214)。注意 `Out_CrcErr <= Pl_CrcErr` 是**不随握手**的单周期脉冲（文档强调）。

**FLAG 模式：pl_stage 透传标志位**。把 `Pl_CrcErr`、`In_Last`、数据拼成 `DataWidth_g+2` 位过一级 pl_stage：

[src/base/vhdl/olo_base_crc_check.vhd:216-243](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L216-L243)。此模式下 `Out_CrcErr` **随握手**输出。

**CRC 引擎实例化**（与 append 完全一致的参数透传）：

[src/base/vhdl/olo_base_crc_check.vhd:168-185](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L168-L185)。注意只映射了 `Out_Crc`，引擎的 `Out_Valid`/`In_Ready` 未连（其 `Out_Ready` 用默认 `'1'`），所以引擎始终接纳输入、不构成反压瓶颈。

#### 4.3.4 代码实践（跑测试 + 读 FSM）

**目标**：分别运行 DROP 与 FLAG 模式，确认行为差异。

1. 运行 check 实体的测试台：
   ```bash
   cd sim
   python run.py --ghdl olo_base_crc_check_tb
   ```
   该 TB 在 `sim/test_configs/olo_base.py` 里对 `Mode_g` 取 `DROP` 与 `FLAG` 各注册了一组配置（见配置文件 `olo_base_crc_check_tb` 一段）。
2. 阅读 [src/base/vhdl/olo_base_crc_check.vhd:188-243](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L188-L243)，对照确认：DROP 用包 FIFO、FLAG 用 pl_stage。
3. **观察现象（推理）**：对于同一个好包，FLAG 模式几乎「边收边发」（延迟低），DROP 模式必须「收完整包、比完 CRC」才输出第一个字（延迟高，且 `FifoDepth_g` 至少要 ≥ 2×最大包长）。
4. **预期结果**：两组用例全部通过。
5. **待本地验证**：FIFO 满时的反压行为需在波形中观察。

#### 4.3.5 小练习与答案

**练习 1**：DROP 模式下，为什么输出延迟比 FLAG 模式高？
> **答案**：DROP 必须先收完整包、比完 CRC 才能决定该包去留，所以首拍数据要缓存到包尾之后才输出（见 [L188-L214](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L188-L214) 及文档「Details about DROP mode」）。FLAG 不需要缓存整包，只用一级 pl_stage，所以可边收边发。

**练习 2**：两种模式下 `Out_CrcErr` 的时序约定有何不同？
> **答案**：DROP 模式下 `Out_CrcErr` 是**不随握手**的单周期脉冲（坏包被丢、脉冲只指示「发生过一次丢弃」）；FLAG 模式下 `Out_CrcErr` **随握手**输出，附着在坏包最后一拍上（见文档对两种模式的说明）。

### 4.4 包保护链路：append 与 check 串接

#### 4.4.1 概念说明

把 append（写侧）与 check（读侧）背靠背串起来，就构成一条完整的包保护链路：原始包 → append 追加 CRC →（中间可有任何 AXI-S 通路）→ check 复算并比对 → 好包通过、坏包被丢弃或标记。两侧必须使用**完全相同**的 CRC 参数（多项式、初值、位序、字节序、翻转、XOR 掩码），否则即使数据完好也会被判为坏包。

这条链路是本讲的综合载体：它同时复用了 4.1 的引擎、4.2 的追加、4.3 的校验，以及 u3-l2 的包 FIFO（DROP 模式）。

#### 4.4.2 核心流程

1. 用户数据进入 `olo_base_crc_append`，输出端每个包多一拍 CRC。
2. 中间通路（可能是 FIFO、跨时钟域、线路）把含 CRC 的包送到 `olo_base_crc_check`。
3. check 在 `In_Last` 拍比对 CRC：相等则整包正常输出；不等则按模式丢弃或标记。
4. 若人为在中间通路翻转某些位（错误注入），check 应检出并丢弃/标记该包。

#### 4.4.3 源码精读（测试台即样板）

仓库已经提供了这条链路的测试台 `olo_base_crc_append_check_tb`，它正是综合实践要用的样板。关键设计：

**DUT 串接**：append 的输出经一个「可被注入错误」的中间通道接到 check：

- append 实例化见 [test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd:203-219](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L203-L219)（16 位数据、多项式 `x"0589"`，即 CRC-16/DECT-R 系列）。
- check 实例化见 [test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd:221-240](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L221-L240)，`Mode_g` 由 TB 泛型 `CheckMode_g` 传入。

**错误注入（XOR 翻转）**：中间通道数据 = 正常数据 XOR 一个「翻转掩码」，掩码由一个 AXI-S master（`Bitflip_c`）逐拍提供，且只在通道真正握手的那拍生效：

[test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd:258-270](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L258-L270)：

```vhdl
ChnlData <= Chnl_Data xor ChnlFlip;          -- 逐拍 XOR 注入
ChnlBeat <= Chnl_Valid and Chnl_Ready;       -- 仅握手拍消费一个掩码
```

**发包过程 `testPacket`**：数据拍一律注入全 0 掩码（不破坏数据）；CRC 拍根据 `crcError` 参数决定注入全 0（好包）还是全 1（翻转整个 CRC 字，制造坏包）：

[test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd:82-106](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L82-L106)。其中坏包的判定：DROP 模式下该包不应出现在输出（不 `check_axi_stream`），FLAG 模式下该包出现但末拍 `tuser="1"`（见 `testPacket` 内 [L91-L98](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L91-L98)）。

**测试用例**：`no-error-x5`（5 个好包全过）、`two-errors`（4 个包中第 2、3 个坏）、`random-packets`（50 个随机包、约 40% 坏），见 [L157-L182](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L157-L182)。

#### 4.4.4 代码实践（综合实践的简化版）

**目标**：运行这条链路、确认错误包被正确处置。

1. 运行（DROP 与 FLAG 两种 `CheckMode_g` 都已被 `test_configs` 注册）：
   ```bash
   cd sim
   python run.py --ghdl olo_base_crc_append_check
   ```
2. 定位 `two-errors` 用例 [test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd:167-172](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd#L167-L172)：4 个包依次为 好、坏、坏、好。
3. **观察现象（推理 + 波形）**：
   - DROP 模式：输出端只见第 1、第 4 个包；第 2、3 个包被丢弃，且各产生一个 `Out_CrcErr` 单周期脉冲。
   - FLAG 模式：4 个包都输出，但第 2、3 个包末拍 `Out_CrcErr='1'`。
4. **预期结果**：所有用例通过。这等价于「正确包全部通过、错误包全部被丢弃/标记」。
5. **待本地验证**：脉冲的确切周期与包间相对时序需在波形中核对。

#### 4.4.5 小练习与答案

**练习 1**：若把 append 与 check 配成**不同**的多项式，好包会被怎样处理？
> **答案**：会被判为坏包。因为 check 复算用的多项式与 append 追加时用的不一致，算出的 CRC 几乎必然不同，于是 [L135](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc_check.vhd#L135) 的不等成立、`Pl_CrcErr` 拉高。两侧 CRC 参数必须完全一致。

**练习 2**：测试台用「翻转整个 CRC 字」来造坏包。如果改成「翻转某个**数据**拍的一位」，check 还能检出吗？
> **答案**：能。任何数据位的翻转都会改变复算出的 CRC，使它与 append 追加的（正确的）CRC 不符，从而被判坏。这也是 CRC 的价值——它保护的是整个包的数据完整性，不限于 CRC 拍本身。

## 5. 综合实践

**任务**：基于 `olo_base_crc_append_check_tb` 这个样板，亲手构建并验证一条「数据位翻转」注入的错误检测链路，把本讲四个模块串起来。

1. **复制样板**：把 `test/base/olo_base_crc_check/olo_base_crc_append_check_tb.vhd` 复制为一个新的 TB（例如 `olo_base_crc_mychain_tb.vhd`），并放到自己的试验目录（不要改原文件）。记得在对应 `test_configs` 里为它注册一个 `test_bench`（参照 [sim/test_configs/olo_base.py:346-350](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L346-L350) 的写法）。
2. **改造错误注入**：把 `testPacket` 里「只在 CRC 拍注入全 1」改为「在某个**数据**拍注入一个单 bit 翻转掩码」（例如 `x"0001"`）。确认 check 仍能检出（参考 4.4.5 练习 2 的推理）。
3. **加一个丢弃计数器**：在 DROP 模式下，用一个计数器统计 `Out_CrcErr` 脉冲个数；发 10 个包、其中 3 个坏，验证计数器最终为 3。
4. **对照两种模式**：分别用 `CheckMode_g = "DROP"` 与 `"FLAG"` 跑一次，在波形里记录：
   - DROP：输出包数 = 好包数；`Out_CrcErr` 脉冲数 = 坏包数。
   - FLAG：输出包数 = 总包数；带 `Out_CrcErr='1'` 的末拍数 = 坏包数。
5. **预期结果**：两种模式下「坏包都被识别」，区别仅在「丢弃 vs 标记」。若计数器与预期不符，先排查两侧 CRC 参数是否一致、错误注入是否对齐到了真正握手的拍（`ChnlBeat`）。
6. **待本地验证**：计数器代码与精确波形需在本机仿真中确认。

> 提示：本实践同时用到 4.1（引擎参数）、4.2（append 的 `In_Beat` 计数时机）、4.3（DROP/FLAG 差异与 `Out_CrcErr` 时序）、4.4（链路与错误注入），是一个把全讲内容「串」起来的小工程。

## 6. 本讲小结

- CRC 是 GF(2) 上的多项式余数，加减即 XOR，可用 LFSR「移位 + 条件 XOR 多项式」逐位硬件实现；`olo_base_crc` 把这套逻辑浓缩在 [L183-L199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_crc.vhd#L183-L199)。
- 一个工业标准 CRC 由「多项式 / 初值 / 位序 / 字节序 / 输出位翻转 / 输出 XOR」六个参数决定，直接对应引擎与 append/check 的泛型，可对照 crccalc.com 配置。
- `olo_base_crc_append` 用 `Data_s/Crc_s` 两态 FSM 在包尾追加一拍 CRC（放低位），输出无气泡、输入每包停顿一拍；CRC 位宽须 ≤ 数据位宽。
- `olo_base_crc_check` 利用引擎「输出晚输入一拍」的特性，在 `In_Last` 拍自然地只对「除 CRC 拍外的所有数据」复算 CRC 并比对。
- check 的 `DROP` 模式用包 FIFO（`DROP_ONLY`）丢坏包、`Out_CrcErr` 为单周期脉冲；`FLAG` 模式用 pl_stage 透传、`Out_CrcErr` 随握手输出且延迟更低。
- append 与 check 必须用**完全相同**的 CRC 参数；二者串接即构成端到端的包完整性保护链路，仓库已提供带 XOR 错误注入的测试台样板。

## 7. 下一步学习建议

- **继续 base 区的数据完整性主题**：阅读 `doc/base/olo_base_crc.md` 末尾的标准 CRC 参数表，并对照 [crccalc.com](https://crccalc.com) 自行配置一个 CRC-32 实例跑通。
- **深入包 FIFO**：本讲 DROP 模式直接依赖 `olo_base_fifo_packet`（u3-l2）。建议回看它的 `DROP_ONLY` 子集与 `In_Drop`/`Out_Repeat` 机制，理解 check 为何选它做缓冲。
- **跨时钟域保护**：若你的 append 与 check 分处不同时钟域，可在二者之间插入 `olo_base_fifo_async`（u3-l1）或 `olo_base_cc_handshake`（u4-l3），把本讲的包保护与跨时钟域传输结合。
- **验证工程化**：本讲的 `olo_base_crc_append_check_tb` 用到了 VUnit 的 AXI-S master/slave 验证组件与 `run("...")` 用例分离，将在 u10-l1 系统讲解。
