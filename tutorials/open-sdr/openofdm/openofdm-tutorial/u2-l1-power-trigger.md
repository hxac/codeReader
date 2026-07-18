# 包检测 power_trigger.v

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `power_trigger` 模块在整条 OFDM 解码流水线里扮演的「守门人」角色，以及它为什么必须走在自相关同步（`sync_short`）之前。
- 读懂 `power_trigger.v` 的三态状态机 `S_SKIP → S_IDLE → S_PACKET`，并解释每一步在做什么。
- 掌握三个设置寄存器 `SR_POWER_THRES` / `SR_POWER_WINDOW` / `SR_SKIP_SAMPLE` 的含义、默认值与运行时修改方式。
- 理解 `setting_reg` 这个 USRP 风格的「配置寄存器原语」是如何按地址匹配写入参数的。
- 分析「连续 N 个低功率样本才解除触发」这一防误触窗口的必要性。

本讲是「前端检测与同步」单元的第一篇，承接 [u1-l5 OFDM 解码流水线总览](u1-l5-decode-pipeline-overview.md) 中提到的流水线第一步——包检测。

## 2. 前置知识

在进入源码之前，先用通俗语言把几个概念讲清楚。

**IQ 采样与「功率」。** 接收机收到的射频信号被下变频后，得到两路实数序列：同相分量 I 与正交分量 Q。OpenOFDM 的输入 `sample_in[31:0]` 把它们打包成一个 32 位字：高 16 位是 I（有符号），低 16 位是 Q（有符号）。信号的「能量/功率」正比于 \(I^2+Q^2\)，工程上常用其平方根 \(\sqrt{I^2+Q^2}\) 作为幅值。本模块做了一个简化：**只用 I 路的绝对值 \(|I|\) 作为功率的近似**，从而完全省掉平方与开方——这是后文源码精读的一个重点。

**为什么需要「包检测」。** 802.11 OFDM 包的开头是一段短前导（short preamble），用来让接收机发现「包来了」。最自然的检测办法是利用短前导的周期性做自相关，但自相关有一个坑：**纯静默段（恒定电平）的自相关值也接近 1**，会误判成前导。因此必须先用一个粗粒度的「能量门限」判断当前是不是「有意义的信号」，只有在能量足够高时，才把后续的精同步模块放行。`power_trigger` 就是这道门。

**状态机。** 硬件里常用一段 `always @(posedge clock)` 配合 `case(state)` 描述「在哪个状态、满足什么条件就跳到哪个状态」。本模块只有 3 个状态，非常适合作为第一次精读硬件状态机的入门例子。

**设置寄存器（setting register）。** FPGA 上很多参数（门限、窗口大小）需要在运行时由 host 机调整，而不是写死在 RTL 里。OpenOFDM 借用 USRP 的「配置总线」：host 拉高 `set_stb`（strobe），同时给出 8 位地址 `set_addr` 和 32 位数据 `set_data`；每个模块内部用若干 `setting_reg` 原语各自「认领」属于自己的地址。这相当于一个分布式寄存器堆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verilog/power_trigger.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v) | 本讲主角：基于 I 路功率门限的包检测模块，含三态状态机。 |
| [verilog/usrp2/setting_reg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v) | 配置寄存器原语：按地址匹配写入、复位时回到默认值。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 定义 `SR_*` 寄存器地址常量与顶层状态码。 |
| [docs/source/detection.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst) | 检测原理文档：解释 power_trigger 与 sync_short 的分工。 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层模块：例化 `power_trigger` 并在主状态机里消费 `trigger` 信号。 |
| [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) | 测试台：演示如何在运行时把 `SR_SKIP_SAMPLE` 改写成 0。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲「为什么需要包检测」（文档视角），再讲配置原语 `setting_reg`，然后精读 `power_trigger` 的核心三态机，最后单独讲防误触窗口。

### 4.1 包检测的定位：为什么自相关之前要先过能量门

#### 4.1.1 概念说明

802.11 OFDM 包的短前导由 10 段重复的、每段 16 个 IQ 样本组成，在 20 MSPS 采样率下共 160 个样本、持续 8 µs。理想的检测器是计算一个延迟自相关度量：把当前样本与 16 个样本之前的样本相乘（取共轭），如果信号每 16 个样本重复一次，这个度量就接近 1。

但是，**静默段（常数电平）同样满足「每 16 个样本重复一次」**——常数信号的自相关也接近 1。如果直接拿自相关做判决，接收机会在静默段就误报「前导到来」，把后续整条解码流水线带偏。

所以 OpenOFDM 的策略是分两层：

1. **第一层 `power_trigger`（本讲）**：粗粒度能量门限，只回答「现在有没有值得处理的信号」。
2. **第二层 `sync_short`（下一讲）**：在能量够高的前提下，再用自相关精确确认短前导。

#### 4.1.2 核心流程

`power_trigger` 对外的行为非常简单：

```text
sample_in ──▶ [ 取 |I| ] ──▶ [ 与门限比较 ] ──▶ trigger (1 bit)
                                       ↑
                            SR_POWER_THRES / SR_POWER_WINDOW / SR_SKIP_SAMPLE
```

- 复位后先跳过若干初始样本（`SR_SKIP_SAMPLE`），避开硬件上电瞬间的毛刺。
- 进入空闲态后，只要 \(|I|\) 超过门限 `SR_POWER_THRES`，就拉高 `trigger`。
- `trigger` 保持高，直到 \(|I|\) 连续低于门限超过 `SR_POWER_WINDOW` 个样本，才回到低。

#### 4.1.3 源码精读

文档 [detection.rst:L12-L37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst#L12-L37) 用三句话概括了这个模块的意图：

> after skipping certain number of initial samples, it waits for significant power increase and triggers the `trigger` signal upon detection. The `trigger` signal is asserted until the power level is smaller than a threshold for certain number of continuous samples.

这段话直接对应源码里的三态机。文档还在 [detection.rst:L21-L31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst#L21-L31) 解释了为什么要先做能量检测：因为自相关对「恒定电平」这种「无意义信号」也会给出近 1 的度量，必须先用能量门把它们挡掉。

> 关于自相关度量的数学形式（属于下一讲 `sync_short`，这里只作背景）：
> \[ \mathrm{corr}[i] = \frac{\left\lvert\sum_{k=0}^{N} S[i+k]\cdot\overline{S[i+k+16]}\right\rvert}{\sum_{k=0}^{N} S[i+k]\cdot\overline{S[i+k]}} \]
> 其中 \(S\) 是复数样本，\(\overline{S}\) 是其共轭。当信号每 16 样本重复时分子分母接近相等，比值接近 1。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「能量门 → 自相关」两层检测的直觉。
2. **步骤**：打开 [detection.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst)，阅读 `Power Trigger` 与 `Short Preamble Detection` 两节。
3. **观察**：注意文档里提到「silence also repeats itself (at arbitrary interval)」这句——它正是 `power_trigger` 存在的根本理由。
4. **预期结果**：你能用自己的话说出「如果没有 power_trigger，sync_short 会在静默段误报」这一结论。
5. **待本地验证**：可结合下一讲把 `short_preamble.png` / `corr.png` 两张图对照看（图片随仓库 docs 提供）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `power_trigger` 整个删掉，直接让 `sync_short` 接收原始样本，最可能出什么问题？

**参考答案**：静默段的恒定电平自相关值也接近 1，`sync_short` 会在没有真实包时就误判「短前导到来」，后续 `sync_long`、解码等模块会被错误启动，整条流水线被带偏。`power_trigger` 的能量门正是用来先过滤掉这种静默段。

---

### 4.2 配置寄存器原语 setting_reg

#### 4.2.1 概念说明

`power_trigger` 里的三个参数（门限、窗口、跳过样本数）都不是写死的常数，而是通过 `setting_reg` 这个原语暴露成「可被 host 运行时修改的寄存器」。`setting_reg` 来自 USRP 平台（`verilog/usrp2/setting_reg.v`），是一个高度可复用的小组件：

- 用参数 `my_addr` 声明「我监听哪个地址」；
- 用参数 `width` 声明寄存器位宽；
- 用参数 `at_reset` 声明复位后的默认值。

理解了它，你就能看懂项目里所有 `SR_*` 配置寄存器是怎么挂上去的。

#### 4.2.2 核心流程

配置总线由三根信号组成，一次「写寄存器」的时序是：

```text
clk   ──┐      ┌──┐      ┌──┐
        └──────┘  └──────┘
stb   ────────────┐                 <- set_stb 拉高一拍
addr  =  不要紧   = SR_POWER_THRES   <- set_addr 给出目标地址
data  =  不要紧   = 200              <- set_data 给出新值
                         ▲
              地址匹配的 setting_reg 在这一拍把 out 更新为 200
```

关键点：**所有 `setting_reg` 共享同一组 `stb/addr/data`，但只有 `my_addr==addr` 的那一个会真正写入**，其余的 `out` 保持不变。这就是「分布式寄存器堆」的实现方式。

#### 4.2.3 源码精读

`setting_reg` 的全部逻辑只有一个 `always` 块，见 [setting_reg.v:L27-L41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/setting_reg.v#L27-L41)：

```verilog
always @(posedge clk)
  if(rst) begin
     out <= at_reset;
     changed <= 1'b0;
  end else
    if(strobe & (my_addr==addr)) begin
       out <= in;
       changed <= 1'b1;
    end else
      changed <= 1'b0;
```

读法：

- **复位优先**：`rst` 一来，`out` 回到 `at_reset` 默认值，`changed` 清零。这正是 `power_trigger` 里 `at_reset(100)` / `at_reset(80)` / `at_reset(5000000)` 这些默认值生效的地方。
- **地址匹配写入**：`strobe & (my_addr==addr)` 同时成立时，把 `in`（即 `set_data`）锁存进 `out`，并拉高 `changed` 一拍。
- **changed 信号**：`changed` 只在「本次真的写入了」的那一拍为 1，其余拍为 0。`power_trigger` 用它来检测「host 是否刚改了 `SR_SKIP_SAMPLE`」，从而决定是否重新进入跳过态（见 4.3.3）。

地址常量定义在 [common_params.v:L16-L19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L16-L19)：

```verilog
localparam SR_POWER_THRES   = 3;
localparam SR_POWER_WINDOW  = 4;
localparam SR_SKIP_SAMPLE   = 5;
```

而 `power_trigger` 里挂了三个 `setting_reg` 实例，见 [power_trigger.v:L35-L47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L35-L47)：

| 实例 | 地址 | 位宽 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `sr_0` | `SR_POWER_THRES` (3) | 16 | 100 | 触发门限：\(\|I\|>{}\)此值即认为有信号 |
| `sr_1` | `SR_POWER_WINDOW` (4) | 16 | 80 | 解除触发所需的连续低功率样本数 |
| `sr_2` | `SR_SKIP_SAMPLE` (5) | 32 | 5000000 | 上电后跳过的初始样本数 |

> 注意 `SR_SKIP_SAMPLE` 的默认值 5,000,000：在 20 MSPS 下对应 250 秒，显然是为真实硬件上电稳定期准备的。仿真时测试台会把它改写成 0（见 4.2.4）。

#### 4.2.4 代码实践

1. **目标**：亲眼看到「地址匹配写入」在仿真中生效。
2. **步骤**：打开 [dot11_tb.v:L107-L114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114)，看测试台如何把 `SR_SKIP_SAMPLE` 改成 0：
   ```verilog
   set_stb = 1;
   # 20
   set_addr = SR_SKIP_SAMPLE;
   set_data = 0;
   # 20 set_stb = 0;
   ```
   也就是说：先拉高 `set_stb`，再给出地址 5、数据 0，保持一拍后撤销。
3. **观察**：在波形（`dot11.vcd`）里同时看 `set_stb`、`set_addr`、`set_data` 与 `power_trigger_inst.sr_2.out`（即 `num_sample_to_skip`）。
4. **预期结果**：在 `set_stb` 拉高、`set_addr==5` 那一拍之后，`num_sample_to_skip` 从复位默认的 5,000,000 变成 0，`power_trigger` 因此几乎立刻进入 `S_IDLE`，而不是空等 250 秒。
5. **待本地验证**：不同 iverilog 版本下信号层级名可能略有差异，若在 gtkwave 里找不到 `sr_2.out`，可在 `$dumpvars` 展开后搜索 `num_sample_to_skip`。

#### 4.2.5 小练习与答案

**练习 1**：为什么三个 `setting_reg` 可以共用同一组 `set_stb/set_addr/set_data`，却不会互相干扰？

**参考答案**：因为每个实例有不同的 `my_addr`（分别是 3、4、5），只有 `my_addr==addr` 的那一个才会执行 `out <= in`。对其它实例而言 `strobe & (my_addr==addr)` 为假，`out` 保持不变，仅 `changed` 被清零。

**练习 2**：如果 host 想在运行时把功率门限从 100 调到 200，应该怎样驱动配置总线？

**参考答案**：拉高 `set_stb`，令 `set_addr=SR_POWER_THRES`（即 3）、`set_data=200`，保持一个时钟周期后撤销 `set_stb`。地址为 3 的 `sr_0` 实例会在该拍把 `power_thres` 更新为 200。

---

### 4.3 power_trigger 模块：功率门限与三态状态机

#### 4.3.1 概念说明

有了 `setting_reg` 提供的三个可调参数，`power_trigger` 的核心就是一个三态状态机：

- `S_SKIP`：上电后跳过 `num_sample_to_skip` 个样本，避开硬件稳定期。
- `S_IDLE`：空闲守候，一旦 \(|I|\) 超过门限就触发。
- `S_PACKET`：认为包正在到来，保持 `trigger=1`，直到能量持续跌落才解除。

这里有一个值得注意的工程简化：模块**只取 I 路**（`sample_in[31:16]`）的绝对值作为功率代理，而不计算 \(\sqrt{I^2+Q^2}\)。这样完全省掉了平方与开方，代价是对「I 路很小、Q 路很大」的信号不够敏感——但对于前导这种能量分布在两路上的信号，配合一个保守的门限是够用的。

#### 4.3.2 核心流程

把 I 路取绝对值用的是二进制补码的小技巧：负数 `x` 的绝对值是 `~x + 1`（按位取反再加一，即求补）。状态机每收到一个有效样本（`sample_in_strobe` 为 1）才推进一次：

```text
            reset
              │
              ▼
         ┌─────────┐  sample_count > num_sample_to_skip
         │ S_SKIP  │ ────────────────────────────────────────┐
         │ 计数跳过 │                                          │
         └─────────┘                                          ▼
              ▲                                      ┌─────────────┐
              │                                      │   S_IDLE    │
              │ num_sample_changed                   │ 守候门限    │
              │ (host 改了跳过数)                     └─────────────┘
              │                                            │ |I| > power_thres
              │                                            ▼
         ┌───────────┐  连续低功率 > window_size     ┌─────────────┐
         │ S_PACKET  │ ◀──────────────────────────── │   S_PACKET  │
         │ trigger=1 │                               │  trigger=1  │
         └───────────┘                               └─────────────┘
```

注意：`S_PACKET` 里的 `sample_count` 专门用来数「连续低功率样本」，任何一次 \(|I|\ge\) 门限都会把它清零。这是下一节要讲的防误触窗口。

#### 4.3.3 源码精读

**端口**见 [power_trigger.v:L1-L15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L1-L15)：除了时钟/使能/复位与配置总线，就是 32 位 `sample_in` + `sample_in_strobe` 输入，以及单比特 `trigger` 输出。

**取 I 路绝对值**——注意它只用了高 16 位：

[power_trigger.v:L31-L32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L31-L32)
```verilog
wire [15:0] input_i = sample_in[31:16];
reg  [15:0] abs_i;
```

真正的取绝对值发生在 always 块里，[power_trigger.v:L56-L57](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L56-L57)：
```verilog
end else if (enable & sample_in_strobe) begin
    abs_i <= input_i[15]? ~input_i+1: input_i;
```
`input_i[15]` 是符号位：为 1 表示负数，取 `~input_i+1` 求补得到绝对值；为 0 直接用原值。`abs_i` 是寄存器，所以这里的比较用的是「上一个样本」的绝对值——这一点对后文测量延迟很关键。

**三态机主体**，[power_trigger.v:L58-L95](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L58-L95)：

```verilog
case(state)
    S_SKIP: begin
        if(sample_count > num_sample_to_skip) begin
            state <= S_IDLE;          // 跳够数，转空闲
        end else begin
            sample_count <= sample_count + 1;
        end
    end

    S_IDLE: begin
        if (num_sample_changed) begin
            sample_count <= 0;         // host 改了跳过数，重新跳过
            state <= S_SKIP;
        end else if (abs_i > power_thres) begin
            trigger <= 1;              // 发现能量，触发！
            sample_count <= 0;
            state <= S_PACKET;
        end
    end

    S_PACKET: begin
        if (num_sample_changed) begin
            sample_count <= 0;         // 配置变更，回到跳过态
            state <= S_SKIP;
        end else if (abs_i < power_thres) begin
            if (sample_count > window_size) begin
                trigger <= 0;          // 连续低功率够久，解除触发
                state <= S_IDLE;
            end else begin
                sample_count <= sample_count + 1;  // 继续数低功率样本
            end
        end else begin
            sample_count <= 0;         // 仍高功率，清零连续低计数
        end
    end
endcase
```

读法要点：

1. 整段逻辑包在 `else if (enable & sample_in_strobe)` 里——**没有 strobe 就不推进**，这是项目里反复出现的「数据 + strobe」握手风格。
2. `num_sample_changed` 在 `S_IDLE` 和 `S_PACKET` 里都被检查：只要 host 刚改过 `SR_SKIP_SAMPLE`，模块就无条件回到 `S_SKIP` 重新跳过。这保证运行时调整跳过数后行为可预期。
3. `trigger` 是 `reg`，赋值是非阻塞（`<=`），所以 `trigger=1` 要到下一个时钟边沿才对外可见。

**顶层如何消费 trigger**：`dot11.v` 在 [dot11.v:L257-L270](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L257-L270) 例化了 `power_trigger_inst`，主状态机在 `S_WAIT_POWER_TRIGGER` 态轮询它，见 [dot11.v:L464-L476](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L464-L476)：
```verilog
S_WAIT_POWER_TRIGGER: begin
    ...
    if (power_trigger) begin
        sync_short_reset <= 1;
        state <= S_SYNC_SHORT;        // 把后续同步模块放行
    end
end
```
而在 [dot11.v:L483-L486](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L483-L486) 与 [dot11.v:L512-L514](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L512-L514)，若 `~power_trigger`（能量提前跌落），顶层会回到 `S_WAIT_POWER_TRIGGER` 等下一个包。

#### 4.3.4 代码实践（动手改默认值）

这是本讲的主实践，**请在本地副本上临时修改、观察后还原**（不要把改动提交进源码）。

1. **目标**：体会门限高低对 `trigger` 出现时刻的影响，并测量从样本输入到 `trigger` 置位的延迟。
2. **步骤**：
   - 打开 [verilog/power_trigger.v:L35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L35)，把 `SR_POWER_THRES` 的默认值 `.at_reset(100)` 临时改成 `.at_reset(400)`，重新 `make simulate`。
   - 用 gtkwave 打开 `sim_out/dot11.vcd`，把 `sample_in[31:16]`（I 路）与 `power_trigger_inst.trigger` 放在同一视图。
3. **观察**：
   - 门限从 100 提到 400 后，`trigger` 第一次拉高的时刻应该**明显后移**（需要等 I 路幅度长到更大才触发）；若改回更小的值（如 50），`trigger` 会**提前**出现，甚至可能在真实包到来前就被噪声拉高。
   - 同时观察：`trigger` 拉高的时刻比「I 路首次超过门限」的时刻晚了大约 1～2 个 `sample_in_strobe` 周期。原因是 `abs_i` 与 `trigger` 都是寄存器，存在两级流水延迟。
4. **预期结果**：门限越高，触发越晚、越保守（漏检风险上升）；门限越低，触发越早、越激进（误检风险上升）。延迟拍数（待本地验证）：约为 2 个样本周期量级，请你用波形里的游标量出精确值。
5. **还原**：测量完成后把 `.at_reset(100)` 改回原值。
6. **替代方案（不改源码）**：参照 [dot11_tb.v:L107-L114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114) 的写法，在测试台里追加一段对 `SR_POWER_THRES`（地址 3）的运行时写入，效果等价且完全不触碰源码——这其实更贴近真实上板场景。

> 说明：本实践给出的「延迟约 2 个样本周期」是基于源码两级寄存器（`abs_i`、`trigger`）的原理推断；精确拍数依赖测试台喂样节拍，请以本地波形测量为准。

#### 4.3.5 小练习与答案

**练习 1**：模块为什么只比较 `abs_i`（I 路绝对值），而不是真正的功率 \(I^2+Q^2\)？

**参考答案**：为了省资源。计算 \(I^2+Q^2\) 需要两个乘法器和一个加法器，而取 \(|I|\) 只需要一次补码求补（取反加一）和一个比较器。对于能量较均匀分布在前导上的信号，用一个保守的门限用 \(|I|\) 做代理已经足够完成「有没有信号」的粗判。

**练习 2**：`abs_i` 是 `reg`（寄存器），这对触发时刻有什么影响？

**参考答案**：`abs_i` 在当前 strobe 拍被更新为「当前样本」的绝对值，但同一拍 `case` 里做比较时读到的是「上一个样本」的绝对值（非阻塞赋值语义）。因此从「高功率样本到达」到「比较命中」之间天然多出大约一个样本周期，再加上 `trigger` 本身也是寄存器，总共约有 2 个样本周期的延迟。

---

### 4.4 防误触窗口与触发解除

#### 4.4.1 概念说明

假设已经触发进入了 `S_PACKET`。此时若信号出现一个短暂的低谷（例如前导段之间的轻微凹陷、或多径造成的瞬时衰减），理想行为应该是「忽略它，继续认为包还在」，而不是立刻解除触发、把流水线打断。

`power_trigger` 用 `SR_POWER_WINDOW`（默认 80）实现这一点：**只有当 \(|I|\) 连续低于门限超过 80 个样本时，才解除触发**。任何一次高功率样本都会把计数器清零。

换算一下：在 20 MSPS 下，80 个样本 = 4 µs。短前导全长 8 µs（160 样本），所以 4 µs 的窗口足以「骑过」大多数短暂凹陷。

#### 4.4.2 核心流程

`S_PACKET` 态里的判定可以写成这样的伪代码：

```text
on each strobe:
    if host 改了 skip 参数:
        回到 S_SKIP
    elif |I| < power_thres:           # 本拍低功率
        if sample_count > window_size:   # 已经连续低超过窗口
            trigger <= 0
            回到 S_IDLE
        else:
            sample_count += 1            # 累计连续低样本数
    else:                              # 本拍高功率
        sample_count <= 0               # 清零，重新开始数
```

注意条件用的是严格大于 `sample_count > window_size`，且 `sample_count` 从 0 起算，所以实际需要 `window_size + 1` 个连续低样本才会真正解除触发——这是一个典型的「差一」(off-by-one) 细节，做参数调节时要心里有数。

#### 4.4.3 源码精读

对应代码就在 `S_PACKET` 分支，[power_trigger.v:L79-L94](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L79-L94)：

```verilog
S_PACKET: begin
    if (num_sample_changed) begin
        sample_count <= 0;
        state <= S_SKIP;
    end else if (abs_i < power_thres) begin
        // go back to idle for N consecutive low signals
        if (sample_count > window_size) begin
            trigger <= 0;
            state <= S_IDLE;
        end else begin
            sample_count <= sample_count + 1;
        end
    end else begin
        sample_count <= 0;
    end
end
```

注释 `go back to idle for N consecutive low signals` 一语道破窗口的作用。结合 4.3.3 里顶层的消费方式——`S_SYNC_SHORT`/`S_SYNC_LONG` 态一旦发现 `~power_trigger` 就退回 `S_WAIT_POWER_TRIGGER`（[dot11.v:L483](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L483)、[dot11.v:L512](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L512)）——就能理解：如果没有这个窗口，任何一次瞬时低谷都会让顶层误判「包结束了」，从而丢弃正在处理的包。

#### 4.4.4 代码实践

1. **目标**：观察窗口大小对「触发解除时刻」的影响。
2. **步骤**：在本地副本把 [power_trigger.v:L40](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/power_trigger.v#L40) 的 `SR_POWER_WINDOW` 默认值 `.at_reset(80)` 临时改成 `.at_reset(5)`，重新仿真；再改成 `.at_reset(200)` 仿真一次。
3. **观察**：用 gtkwave 看 `trigger` 从 1 跌回 0 的位置。窗口为 5 时，`trigger` 在信号一出现短暂低谷就很快掉下来；窗口为 200 时，`trigger` 会一直拖到很晚才解除（甚至可能拖过整个短前导）。
4. **预期结果**：窗口越小，触发越「毛刺」（容易中途误解除）；窗口越大，触发越「黏」（包结束后还迟迟不释放，可能错过紧接着的下一个包）。默认值 80 是两者的折中。
5. **待本地验证**：精确的解除延迟取决于样本本身的功率包络，请以本地波形为准；测完记得把默认值还原回 80。

#### 4.4.5 小练习与答案

**练习 1**：把 `SR_POWER_WINDOW` 设得非常大（比如 10000），会带来什么副作用？

**参考答案**：触发会过度「黏住」——当一个真实包结束后，由于需要连续 10000 个低功率样本才解除，`trigger` 会长时间保持高电平，导致接收机在很长一段时间内无法检测紧随其后的下一个包（隐藏终端/连发包场景下吞吐下降）。

**练习 2**：代码里判定解除用的是 `sample_count > window_size` 而不是 `>=`。若 `window_size=80`，实际需要多少个连续低样本才解除？

**参考答案**：81 个。因为 `sample_count` 从 0 开始累加，要满足 `> 80` 必须累加到 81。调参时若希望「恰好 80 个就解除」，应把 `window_size` 设为 79。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个贯穿性小任务：**用运行时配置（不改源码）调节 `power_trigger`，并量化它对整条流水线入口的影响。**

1. **阅读现状**：确认 [dot11_tb.v:L107-L114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114) 已经通过配置总线把 `SR_SKIP_SAMPLE` 写成 0。解释为什么这一步对仿真必不可少（提示：默认 5,000,000 在 20 MSPS 下意味着多久？）。
2. **新增运行时写入**：仿照那段代码，在测试台初始化段追加对 `SR_POWER_THRES`（地址 3）的写入，分别试 `set_data = 50` 与 `set_data = 400`。
3. **量化触发时刻**：每次仿真后，打开 `sim_out/power_trigger.txt`（由 [dot11_tb.v:L161-L163](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L161-L163) 落盘，格式为 `$time/2, power_trigger`），读出 `trigger` 首次为 1 的时间戳；与默认门限 100 时的结果对比。
4. **量化对下游的影响**：再打开 `sim_out/short_preamble_detected.txt`，观察不同门限下「短前导确认」的时刻是否随之提前/延后或丢失。若门限过高导致 `short_preamble_detected` 一直为 0，说明 `power_trigger` 过于保守、把整条流水线饿死了。
5. **交付**：写一份简短结论，包含「门限值 → trigger 首次置位时刻 → 是否最终解码成功（看 `sim_out/byte_out.txt` 是否非空）」的对照表。

这个任务把你学过的「配置总线 → setting_reg → 三态机 → 顶层消费」整条链路走了一遍，是进入下一讲 `sync_short` 前最好的检验。

## 6. 本讲小结

- `power_trigger` 是整条 OFDM 解码流水线的第一道门，用粗粒度能量门限回答「现在有没有值得处理的信号」，避免自相关同步在静默段误报。
- 它**只用 I 路绝对值 \(|I|\)** 作为功率代理，靠补码求补（`~x+1`）取绝对值，省掉了乘法与开方。
- 核心是一个三态状态机：`S_SKIP`（跳过初始样本）→ `S_IDLE`（守候门限）→ `S_PACKET`（保持触发）。
- 三个设置寄存器 `SR_POWER_THRES`(门限=100) / `SR_POWER_WINDOW`(解除窗口=80) / `SR_SKIP_SAMPLE`(跳过=5000000) 都挂载在 `setting_reg` 原语上，可被 host 运行时修改。
- 防误触窗口要求**连续** `window_size+1` 个低功率样本才解除触发，任何一次高功率样本都清零计数器，从而骑过短暂凹陷。
- `trigger` 经 `abs_i`、`trigger` 两级寄存器输出，相对样本输入有约 2 个样本周期的延迟（精确值待本地波形验证）。

## 7. 下一步学习建议

下一讲 [u2-l2 短训练序列同步 sync_short.v](u2-l2-sync-short.md) 会紧接着本讲展开：在 `power_trigger` 放行之后，`sync_short` 如何用延迟自相关（本讲背景里给出的 \(\mathrm{corr}[i]\) 公式）精确确认短前导，并初步估计相位偏移。建议在进入下一讲前：

- 务必把本讲的「运行时改写 `SR_POWER_THRES`」实践跑通，建立起「门限 ↔ 触发时刻」的直觉。
- 重读 [detection.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst) 中 `Short Preamble Detection` 一节，预习自相关度量如何把固定阈值 0.75 转化为移位比较。
- 顺便浏览 [verilog/complex_to_mag_sq.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v) 与 [verilog/moving_avg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v)，它们是 `sync_short` 计算自相关时会用到的复数运算与滑动平均原语。
