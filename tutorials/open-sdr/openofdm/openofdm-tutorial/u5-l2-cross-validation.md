# 交叉验证框架 test.py

## 1. 本讲目标

本讲是「验证、工具链与样本数据」单元的第二篇，承接 [u5-l1 Python 参考解码器 decode.py](u5-l1-python-reference-decoder.md)。u5-l1 解决的是「标准答案从哪里来」，本讲解决的是「**如何把标准答案和 Verilog 硬件的实际输出逐阶段对账**」。

学完本讲你应当能够：

- 说清 `scripts/test.py` 的三段式编排：先用 `decode.py` 算期望、再调 `iverilog`/`vvp` 跑仿真、最后逐文件比对。
- 解释 `stop` / `num_sample` 是怎么由 SIGNAL 字段的 `length` 与 `rate` 反推出来的，以及它如何作为 `-DNUM_SAMPLE` 传给测试台。
- 掌握 `--no_sim` 调试方式：复用上一次的 `sim_out` 只重跑比对。
- 当某一步比对失败时，能定位到 demod / deinterleave / conv / descramble / byte 中的哪一级，并知道去 `sim_out/*.txt` 与 Python 期望里查证。

---

## 2. 前置知识

本讲假设你已经建立以下认知（若没有，请先读对应讲义）：

- **八步解码流水线与「数据 + strobe」握手**（u1-l5）：硬件各阶段都把结果连同一拍 `strobe` 落到 `sim_out/` 下。
- **dot11_tb 测试台的喂样节拍**（u1-l2、u5-l3）：`$readmemh` 把 `.txt` 内存文件载入 `ram`，`clk_count==4` 实现 100 MHz 时钟 ÷ 20 MSPS 采样 = 5:1 的「每 5 拍一个样本」。
- **decode.py 的期望输出**（u5-l1）：`Decoder.decode_next()` 返回一个 9 元组，其中后 8 项是 SIGNAL、星座点、解调、解交织、卷积解码、解扰、字节、MAC 帧——它们与 `sim_out/` 下的落盘文件一一对应。
- **Python 2 运行环境**（u5-l1）：`decode.py` 与 `test.py` 都是 Python 2 语法（`print` 语句、`scipy.fromfile`），且依赖 `scipy` 与 `wltrace`，Python 3 无法直接运行。

一个关键直觉：**交叉验证 = 同一段射频样本，走两条完全独立的实现（浮点 Python / 定点 Verilog），在每个公共中间观测点上逐比特对账**。只要某一级对得上、下一级对不上，bug 就被锁定在两级之间。`test.py` 就是这台「对账机」的驱动。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `scripts/test.py` | 验证驱动（本讲主角） | 三段式编排、`stop` 计算、逐阶段比对 |
| `scripts/decode.py` | 期望生成（u5-l1 主角，本讲当数据源） | `decode_next()` 返回结构、`RATE_PARAMETERS`/`HT_MCS_PARAMETERS` |
| `scripts/bin_to_mem.py` | 样本格式转换 | 把 `.dat` 二进制 I/Q 转成 `$readmemh` 能读的 `.txt` |
| `verilog/dot11_tb.v` | 仿真测试台（u5-l3 主角，本讲当被测端） | 各级 `$fwrite` 把 strobe 信号落盘到 `sim_out/` |

三条数据流的关系示意：

```
 .dat (int16 I/Q)
   │
   ├──(Python)──> decode.Decoder.decode_next() ──> expected_*  (期望)
   │                      │
   │                      └─ length/rate ──> num_sample ──> stop
   │                                                │
   └──(bin_to_mem)──> .txt ──> $readmemh ──> dot11_tb ──> sim_out/*.txt (实际)
                                                        │
                              expected_*  <─────────────┘
                                       逐阶段 diff
```

---

## 4. 核心概念与源码讲解

### 4.1 test.py 的三段式总流程

#### 4.1.1 概念说明

`test.py` 不是单元测试框架，而是一个**单包端到端对账脚本**：给它一个样本文件，它解码第一个包，把 Python 浮点结果与 Verilog 仿真结果在每个解码阶段逐比特比较。整个流程可以清楚地切成三段：

1. **准备**：读样本、必要时把 `.dat` 转成 `.txt`。
2. **期望**：跑 `decode.Decoder(...).decode_next()` 得到 Python 的「标准答案」，并据此算出要让仿真跑多少个样本（`stop`）。
3. **仿真 + 对账**：调 `iverilog`/`vvp` 跑硬件，读回 `sim_out/*.txt`，与期望逐阶段 diff。

#### 4.1.2 核心流程

```
test()
 ├─ 读 .dat -> samples (复数列表)
 ├─ 若 .txt 不存在或过期 -> 调 bin_to_mem.py 生成
 ├─ decode.Decoder(sample).decode_next()
 │     -> begin, expected_signal, cons, expected_demod_out,
 │        expected_deinterleave_out, expected_conv_out,
 │        expected_descramble_out, expected_byte_out, pkt
 ├─ num_sample = (length*8/rate + 前导开销) * 20
 ├─ stop = begin + num_sample + 320       (或 --stop 指定)
 ├─ if not --no_sim:
 │     rm sim_out/* ; iverilog -DNUM_SAMPLE=stop ... ; vvp dot11.out
 ├─ 读回 sim_out/signal_out.txt ... byte_out.txt
 └─ 逐阶段比对：SIGNAL -> DEMOD -> DEINTER -> CONV -> DESCRAMBLE -> BYTE
```

#### 4.1.3 源码精读

脚本的入口与命令行参数定义在这里——注意两个调试开关 `--no_sim` 与 `--stop`：

[scripts/test.py:L16-L24](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L16-L24) 定义 `sample`（必填）、`--no_sim`（跳过仿真）、`--stop`（指定解码样本数，缺省只解第一个包）。

主函数先把二进制样本读成复数列表（高 16 位 I、低 16 位 Q），并打印样本数：

[scripts/test.py:L32-L35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L32-L35) 用 `scipy.fromfile` 读 int16，再两两配对成 `complex(i, q)`。

随后是一段「懒生成」逻辑：只有当 `.txt` 不存在、或比 `.dat` 源文件更旧时，才调用 `bin_to_mem.py` 重新转换，避免每次重跑都做无谓的格式转换：

[scripts/test.py:L37-L41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L37-L41) 用 `getmtime` 比较新旧，按需调 `bin_to_mem.py`。

> 这一段也是为什么你有时候明明改了样本却看到旧结果——如果 `.txt` 时间戳比 `.dat` 新，`test.py` 不会重新转换。手动 `rm` 一下 `.txt` 即可。

#### 4.1.4 代码实践

**实践目标**：不跑仿真，只确认「准备段」与「期望段」能独立工作。

**操作步骤**：

1. 进入 `scripts/` 目录，确认 Python 2 环境与依赖（`scipy`、`wltrace`）就绪。
2. 用 `--no_sim` 跑一个已有的 conducted 样本（它的 `.txt` 已在仓库里）：
   ```bash
   cd scripts
   python test.py ../testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat --no_sim
   ```
3. 观察脚本前半段打印：`Using file ... (N samples)`、`Decoding...`、`[SIGNAL] ...`、`Stop after N samples`。

**需要观察的现象**：脚本会在 `--no_sim` 下**跳过** `iverilog`/`vvp`，直接去读 `sim_out/`。如果 `sim_out/` 为空或不存在，会在打开 `signal_out.txt` 时抛 `IOError`——这正好验证了「期望段」独立于「仿真段」，且对账段强依赖 `sim_out` 已有产物。

**预期结果**：若 `sim_out/` 里残留着上一次成功仿真的输出，则 `--no_sim` 会直接进入逐阶段比对并打印各阶段结果；若 `sim_out/` 为空，则在读取阶段报错。具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test.py` 要在转 `.txt` 前比较 `getmtime`，而不是无脑每次都转？

**参考答案**：`bin_to_mem` 对大样本是 O(样本数) 的磁盘 IO，很慢；而 `.dat` 没变时 `.txt` 完全可复用。用时间戳判定「源是否比产物新」，只在必要时重转，是把开发循环压短的关键优化。

**练习 2**：`samples = [complex(i, q) for i, q in zip(samples[::2], samples[1::2])]` 这一行如果写成 `zip(samples[::2], samples[::2])` 会怎样？

**参考答案**：Q 路会全部错位成 I 路的值（Q 永远等于 I），复数样本退化为纯实数序列，后续 FFT、星座解调全部错乱，几乎每一步比对都会失败。这说明样本的字节序（I 在前、Q 在后）是整条链路的隐含契约。

---

### 4.2 期望输出：decode_next 与 stop 计算

#### 4.2.1 概念说明

「期望」来自 u5-l1 讲过的 `decode.Decoder.decode_next()`。本模块只关心两件事：①它返回的 9 元组里每一项对应硬件的哪个 `sim_out` 文件；②`test.py` 如何用其中的 `length`/`rate` 反推出「仿真要喂多少个样本」。

#### 4.2.2 核心流程

`decode_next()` 的返回结构（u5-l1 已详述其内部算法，这里只看接口）：

[scripts/decode.py:L457-L458](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L457-L458) 返回 `(glbl_index,) + self.decode(...)`，即「包起始样本号」拼上 `decode()` 的 8 元组。

8 元组与 `sim_out` 文件的对应关系：

| decode.py 返回项 | 含义 | 对应 sim_out 文件 |
| --- | --- | --- |
| `signal` (Signal/HTSignal) | L-SIG / HT-SIG 字段解析 | `signal_out.txt` |
| `cons` | 均衡后星座点（复数） | `equalizer_out.txt`（间接） |
| `demod_out` | 解调比特 | `demod_out.txt` |
| `deinter_out` | 解交织比特 | `deinterleave_out.txt` |
| `conv_out` | Viterbi 卷积解码比特 | `conv_out.txt` |
| `descramble_out` | 解扰比特 | `descramble_out.txt` |
| `data_bytes` | 最终字节 | `byte_out.txt` |
| `pkt` | 解析后的 MAC 帧 | （无对应，仅打印） |

`stop` 的计算是本模块的重点。`test.py` 用 SIGNAL 字段里的 `length`（PSDU 字节数）与 `rate`（Mbps）估算整个包在空口中占多少微秒，再换算成样本数：

[scripts/test.py:L49-L50](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L49-L50) 核心公式（展开如下）。

\[

\text{num\_sample} = \left(\frac{\text{length}\times 8}{\text{rate}} + T_\text{preamble}\right) \times 20

\]

其中：

- `length*8/rate`：数据段时长。`rate` 单位是 Mbps = Mbit/s = bit/µs，故 bits ÷ (bit/µs) = µs。
- \(T_\text{preamble}\)：前导与开销，legacy 取 20 µs，HT 取 40 µs（多了 HT-SIG/HT-STF/HT-LTF）。
- `× 20`：采样率 20 MSPS = 20 样本/µs，把微秒换算成样本数。

随后加上包起点 `begin` 与 320 样本余量得到 `stop`：

[scripts/test.py:L52-L56](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L52-L56) `stop = begin + num_sample + 320`；若用户给了 `--stop`，则取 `min(args.stop, len(samples))`。

这个 `stop` 随后被当作 `-DNUM_SAMPLE=<stop>` 传给测试台（见 4.3），决定仿真喂多少个样本就 `$finish`。

#### 4.2.3 源码精读

期望生成的调用点：

[scripts/test.py:L44-L47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L44-L47) 一次性解包出 9 个变量，后面所有比对都基于它们。

各速率的子载波/比特参数（用于后续按符号切片比对）定义在 `decode.py`：

[scripts/decode.py:L94-L104](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L94-L104) `RATE_PARAMETERS`：legacy 各 rate 的 `(n_bpsc, n_cbps, n_dbps)`——每子载波比特数、每符号编码比特数、每符号数据比特数。

[scripts/decode.py:L106-L115](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L106-L115) `HT_MCS_PARAMETERS`：802.11n MCS 0–7 的同三元组（注意 HT 的 `n_cbps` 是 52/104/...，比 legacy 多 4 个子载波）。

`test.py` 根据 `expected_signal.ht` 选择用哪张表：

[scripts/test.py:L109-L112](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L109-L112) HT 取 `HT_MCS_PARAMETERS[mcs]`，legacy 取 `RATE_PARAMETERS[rate]`，得到后续切片用的 `n_bpsc/n_cbps/n_dbps`。

#### 4.2.4 代码实践

**实践目标**：手算一个 legacy 24 Mbps 包的 `num_sample`，理解公式每一项的量纲。

**操作步骤**：

1. 打开 [scripts/decode.py:L100](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L100)，确认 24 Mbps 对应 `rate=24`。
2. 用任意文本编辑器查看 `testing_inputs/conducted/dot11a_24mbps_..._42.txt` 对应的包，或直接跑 `--no_sim` 让脚本打印 `[SIGNAL]` 行，读出其中的 `length` 字段（PSDU 字节数）。
3. 代入公式：假设 `length = L` 字节，则
   \[
   \text{num\_sample} = \left(\frac{L\times 8}{24} + 20\right)\times 20
   \]
4. 与脚本打印的 `Stop after N samples` 比对（注意脚本最终 `stop = begin + num_sample + 320`，要减去 `begin` 和 320 才是纯 `num_sample`）。

**需要观察的现象**：`Stop after` 减去 `begin` 减 320 后，应当与你手算的 `num_sample` 一致。

**预期结果**：量纲对齐时数值吻合；若相差很多，检查是否把 `length` 当成了比特数（它其实是字节数，所以要 `×8`）。具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 HT 包的前导开销取 40 µs 而 legacy 取 20 µs？

**参考答案**：HT-mixed 包在 legacy 前导（L-STF/L-LTF/L-SIG ≈ 20 µs）之后还追加了 HT-SIG、HT-STF、HT-LTF 等 HT 前导字段，额外的 ~20 µs 让总前导达到约 40 µs。公式用这两个常数近似覆盖前导时长，使 `num_sample` 估算能包住整个包。

**练习 2**：`stop = begin + num_sample + 320` 里的 320 是什么？去掉会怎样？

**参考答案**：320 是「保险余量」样本数（约 16 µs）。因为 `num_sample` 是按理想时长估算的，实际包尾可能有 FCS、尾比特、传播抖动，去掉余量可能导致仿真在包真正结束前就 `$finish`，漏掉末尾几个字节，BYTE 比对就会缺尾巴。320 保证仿真多跑一段，确保整包落盘。

---

### 4.3 仿真执行：iverilog/vvp 调用与 dot11_tb 的落盘约定

#### 4.3.1 概念说明

期望算好后，`test.py` 要让 Verilog 把**同样这段样本**解码一遍，并把每个中间阶段写进 `sim_out/`。它通过命令行直接调 `iverilog`（编译）与 `vvp`（运行），把 `memory_file`（`.txt`）与 `stop` 作为宏注入测试台。测试台 `dot11_tb.v` 则负责：载入样本、按 5:1 节拍喂样、用一组 `$fwrite` 把各级 strobe 信号落盘。

#### 4.3.2 核心流程

```
test.py 仿真段:
 ├─ rm -rfv sim_out/*            (清空旧产物，避免脏数据)
 ├─ iverilog -DDEBUG_PRINT
 │          -DSAMPLE_FILE="....txt"
 │          -DNUM_SAMPLE=<stop>
 │          -c dot11_modules.list dot11_tb.v -o dot11.out
 └─ vvp -n dot11.out             (运行仿真，产物写入 sim_out/)

dot11_tb.v 内:
 ├─ $readmemh(SAMPLE_FILE, ram)
 ├─ clk_count==4 时 sample_in_strobe<=1, sample_in<=ram[addr++]
 ├─ addr==NUM_SAMPLE 时 $finish
 └─ 各级 strobe 有效时 $fwrite 到 sim_out/<name>.txt
```

#### 4.3.3 源码精读

仿真段的全部代码（含 Ctrl-C 时 kill 掉 `vvp` 的保护）：

[scripts/test.py:L58-L81](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L58-L81) 注意三个宏：`-DDEBUG_PRINT`（打开调试打印）、`-DSAMPLE_FILE`（指定 `$readmemh` 读哪个样本）、`-DNUM_SAMPLE`（指定喂多少样本后 `$finish`）。

测试台侧，这两个宏有默认值兜底：

[verilog/dot11_tb.v:L82-L88](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L82-L88) 若命令行没传 `SAMPLE_FILE`/`NUM_SAMPLE`，分别用默认的 24 Mbps 样本与 3000 样本——这就是 u1-l2 里「默认样本」的来源。

样本载入点：

[verilog/dot11_tb.v:L96](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L96) `$readmemh` 把 `.txt` 里每行一个 32 位 hex（高 16 位 I、低 16 位 Q）读入 `ram` 数组。

5:1 喂样节拍（u1-l2 已推导）：

[verilog/dot11_tb.v:L148-L156](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L148-L156) `clk_count` 数到 4 才拉高 `sample_in_strobe` 并取下一个样本，实现 100 MHz ÷ 20 MSPS = 5:1。

落盘文件句柄的开辟：

[verilog/dot11_tb.v:L116-L133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L116-L133) 一组 `$fopen("./sim_out/<name>.txt", "w")` 建好所有探针文件。

决定仿真何时结束：

[verilog/dot11_tb.v:L180-L182](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L180-L182) `addr == NUM_SAMPLE` 时 `$finish`——这正是 `test.py` 把 `stop` 当 `-DNUM_SAMPLE` 传进来的落点。

各级探针的写法（这是本模块最关键的一段，决定了每个 `sim_out` 文件的格式）：

[verilog/dot11_tb.v:L199-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L199-L228) 每个 `if (dot11_state == S_DECODE_DATA && <xxx>_strobe)` 守卫一次 `$fwrite`。逐行看：

- **signal**（L201）：`"%04b %b %012b %b %06b"`——把 L-SIG 的 rate/rsvd/len/parity/tail 五段写成空格分隔的一行；注意它只在 `legacy_sig_stb` 有效时写，**且写在 SIGNAL 阶段**，不在 `S_DECODE_DATA` 守卫内。
- **demod**（L206）：`"%06b\n"`——每个 DATA 符号的 6 比特判决，MSB 在前。
- **deinterleave**（L211）：`"%b%b\n"`——每拍 2 比特，`[0]` 在前 `[1]` 在后。
- **conv / descramble**（L216、L221）：`"%b\n"`——每拍 1 比特。
- **byte**（L226）：`"%02x\n"`——每拍 1 字节，十六进制。

> 这几行的**格式串就是 4.4 比对段的契约**：Python 侧必须按完全相同的位宽与顺序去解析，否则即使硬件没错也会「比对失败」。

#### 4.3.4 代码实践

**实践目标**：确认 `NUM_SAMPLE` 宏确实控制了仿真结束时机，并理解 `sim_out/` 的清空时机。

**操作步骤**：

1. 先确保 `sim_out/` 里**有**上一次的产物（或先正常跑一次）。
2. 用 `--no_sim` 跑（验证它**不会**清空 `sim_out/`，也不会调 `iverilog`）。
3. 再不带 `--no_sim` 跑同一个样本（验证 L60 的 `rm -rfv sim_out/*` 会先清空，然后重新编译运行）。
4. 用 `gtkwave` 打开 `verilog/dot11.vcd`，定位到 `addr` 信号，确认它在到达 `NUM_SAMPLE`（即你看到的 `Stop after N samples`）时仿真结束。

**需要观察的现象**：带仿真的那次，`sim_out/` 里文件的时间戳被刷新；`--no_sim` 那次则完全不动 `sim_out/`。

**预期结果**：`vvp` 运行结束后，`sim_out/` 下应出现 `signal_out.txt`、`demod_out.txt`、`deinterleave_out.txt`、`conv_out.txt`、`descramble_out.txt`、`byte_out.txt` 等文件。具体内容「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果不传 `-DNUM_SAMPLE`，仿真会喂多少样本？为什么这个默认值对长包可能不够？

**参考答案**：默认 `NUM_SAMPLE=3000`（dot11_tb.v L87）。3000 个样本只够覆盖约 150 µs，对于长 PSDU 的大包（例如 1500 字节 @ 6 Mbps 要 ~2 ms）远远不够，会在包还没解完时 `$finish`，导致 `byte_out.txt` 缺失大量字节。`test.py` 用 `length`/`rate` 反推 `stop` 正是为了避免这个问题。

**练习 2**：为什么每个 `$fwrite` 都要加 `dot11_state == S_DECODE_DATA` 守卫（SIGNAL 除外）？

**参考答案**：因为这些探针信号（`demod_out_strobe` 等）在解 SIGNAL 符号时也会跳，而 `test.py` 只比对 DATA 阶段的输出。加上状态守卫，确保落盘的只是 DATA 符号的比特，避免把 SIGNAL 符号的比特混进 `demod_out.txt` 造成对账错位。

---

### 4.4 逐阶段比对与失败定位

#### 4.4.1 概念说明

这是 `test.py` 真正的「价值所在」。它按八步流水线的顺序，把期望与实际**按 OFDM 符号逐段 diff**：SIGNAL → DEMOD → DEINTER → CONV → DESCRAMBLE → BYTE。因为各级是**串行依赖**的，定位 bug 的策略很直接：

> 从前向后找**第一个失败的阶段**。它要么本身有 bug，要么是它的上一级喂错了数据。第一级没过，后面所有的「失败」都不可信（垃圾进垃圾出）。

#### 4.4.2 核心流程

```
读 sim_out 各文件 -> 按 Python 期望的位序/子载波序做对齐变换
 -> SIGNAL 逐字段比对 (rate/rsvd/len/parity/tail)
    失败 -> 直接 return (后面没意义)
 -> DEMOD      按 n_cbps 切符号逐符号 diff
 -> DEINTER    按 n_cbps 切符号逐符号 diff
 -> CONV       按 n_dbps 切符号逐符号 diff
 -> DESCRAMBLE 按 n_dbps 切符号逐符号 diff (先补 7 个 0)
 -> BYTE       逐字节比对 (取 min 长度)
```

#### 4.4.3 源码精读

**SIGNAL 比对**——逐字段，且任一字段错就整体中止（因为 SIGNAL 错了，后面 length/rate 全错，比对无意义）：

[scripts/test.py:L93-L107](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L93-L107) 遍历 `['rate_bits','rsvd','len_bits','parity_bits','tail_bits']`，逐项比期望与实际，错则置 `signal_error`，最终失败 `return`。

注意 L84 的位序翻转 `[c[::-1] for c in f.read().strip().split()]`：Verilog 的 `%b` 是 MSB 先写，而 Python 的 `Signal.rate_bits` 是按接收顺序（bit0 先）存的，所以要把每个 token 字符串反转后再比——这是典型的「位序对齐」细节。

**子载波顺序对齐**（DEMOD 比对前的必要变换）：

[scripts/test.py:L114-L120](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L114-L120) 把每个符号的 `n_cbps` 比特切成两半并**交换前后半**。原因：Python 的 `SUBCARRIERS = range(-26,0)+range(1,27)`，先负后正；而 Verilog equalizer 的输出顺序不同。这个 swap 是把两边子载波顺序对齐的「适配层」——忘了它或 `n_cbps` 取错，DEMOD 会全错。

**DEMOD 比对**：

[scripts/test.py:L138-L151](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L138-L151) 按 `num_symbol = min(len(got), len(expected)) / n_cbps` 个符号逐符号 diff，每个符号打印 Expected/Got 两行，并统计不同比特数。失败时打印 `Demod error at SYM <idx>, diff: <n>`。

**demod_out.txt 的解析**也藏着一个位序技巧：

[scripts/test.py:L129-L133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L129-L133) 每行取末尾 `n_bpsc` 位再反转 `[::-1]`——因为 Verilog 写的是固定 6 位（`%06b`），但实际只有低 `n_bpsc` 位有效（如 BPSK 只 1 位），取末 `n_bpsc` 位再反转回 bit0 先。

**DEINTER / CONV 比对**结构相同，只是切片单位不同（DEINTER 用 `n_cbps`，CONV 用 `n_dbps`）：

[scripts/test.py:L153-L166](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L153-L166) DEINTER 比对。

[scripts/test.py:L168-L181](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L168-L181) CONV 比对（Viterbi 输出）。

**DESCRAMBLE 比对**前先补 7 个 0：

[scripts/test.py:L183-L197](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L183-L197) L183 `descramble_out = '0'*7 + descramble_out`。

> 为什么补 7 个 0？回顾 u3-l6：硬件用「直装法」，把收到的 SERVICE 前 7 bit 直接装入 LFSR，**这 7 拍不产生 `output_strobe`**，所以 `descramble_out.txt` 里没有这 7 位；而 Python 的 `descramble()` 输出是含这 7 位的完整序列。比对时给硬件侧前面补 7 个 0（直装期 LFSR 输出视为 0），两边长度与相位才对齐。

**BYTE 比对**——逐字节，十六进制：

[scripts/test.py:L199-L214](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L199-L214) 读 `byte_out.txt` 成 int 列表，与 `expected_byte_out` 逐字节比，打印 `[i / total] Expect: xx, Got: xx`。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标**：用 `test.py` 跑一个 802.11n 样本，练习「按第一失败阶段定位 bug」的排查方法。

**操作步骤**：

1. 在 `scripts/` 下跑一个 dot11n 样本（注意 HT 包要确保 `stop` 足够大，建议先不带 `--stop` 让脚本自动算）：
   ```bash
   cd scripts
   python test.py ../testing_inputs/conducted/dot11n_65mbps_98_5f_d3_c7_06_27_e8_de_27_90_6e_42.dat
   ```
   > 若该速率样本在你的环境下解不出来，可退而用 `dot11n_6.5mbps_...` 或 `dot11n_13mbps_...`（MCS 0/1，最稳）。
2. 从输出里**自上而下**找第一个打印 `error` / `Wrong` 的阶段。脚本的阶段顺序固定为：`Signal.*` → `DEMOD` → `DEINTER` → `CONV` → `DESCRAMBLE` → `BYTE`。
3. 假设第一失败阶段是 **DEMOD**（最常见的「看似失败」点之一）：
   - 打开 `verilog/sim_out/demod_out.txt`，找到脚本指出的 `SYM <idx>`。
   - 与脚本打印的 `Expected` 行对照，看是**整符号全错**还是**只有几个比特错**。
   - 整符号全错 → 多半是子载波顺序/`n_cbps` 对齐问题（回顾 4.4 的 swap）或上游 equalizer/频偏没收敛。
   - 只有几个比特错 → 多半是星座判决门限边界或信道估计误差，属正常「软判决 vs 硬判决」差异（注意：Python 是浮点最近邻，Verilog 是定点门限，个别比特不同是可能的）。
4. 假设第一失败阶段是 **CONV**（前面 DEMOD/DEINTER 都 `works!`）：
   - 问题锁定在 Viterbi。检查 `conv_out.txt` 与期望的差异分布：若是**零星比特错**，多半是软判决/回溯深度（`tb_depth=35`）差异；若是**大片连续错**，怀疑去穿孔（de-puncture）的 erase 模式或 flush 逻辑（见 u3-l5）。
5. 用 `--no_sim` 反复重跑对账段（改了比对逻辑但不改硬件时），省去重新仿真：
   ```bash
   python test.py ../testing_inputs/conducted/dot11n_6.5mbps_....dat --no_sim
   ```

**需要观察的现象**：脚本会为每个阶段打印 `Expected` / `Got` 两行，并在不匹配时给出 `diff: <n>`（差异比特数）。第一个非零 `diff` 的阶段就是排查起点。

**预期结果**：理想情况下所有阶段都打印 `works!`，最后 `BYTE works!`。实际中 DEMOD 偶现个别比特差异、但 CONV 能纠回来、BYTE 仍全对，是可接受的（因为 Viterbi 本就有纠错能力）。具体输出「待本地验证」。

> 排查口诀：**第一失败阶段之前的阶段都可信，之后的都不可信**。不要在 SIGNAL 失败时去纠结 BYTE 为什么也错。

#### 4.4.5 小练习与答案

**练习 1**：DEMOD 比对时，`num_symbol = min(len(demod_out)/n_cbps, len(expected_demod_out)/n_cbps)` 取了两边的最小值。为什么不直接用期望的符号数？

**参考答案**：硬件实际落盘的比特数可能与期望不完全相等（例如仿真因 `NUM_SAMPLE` 不够而提前结束，或 strobe 多/少了几拍）。取最小值保证比对时两边都有数据，不会因为一边短了就越界。这也意味着**长度不一致本身就是一个信号**——若 `demod_out` 明显比期望短，应先怀疑 `stop` 不够大或前端同步丢包。

**练习 2**：DESCRAMBLE 比对前要补 `'0'*7`，但 BYTE 比对前却不需要类似的补偿。为什么？

**参考答案**：descramble 的 7 位差异源于 LFSR 直装期硬件不输出（数据平面缺 7 位），所以要在比特流层面补；而 byte 是 `bits_to_bytes` 之后的结果，service 字段的 16 位（含那 7 位）在成字节时已被 `skip_bit=9` 等逻辑统一处理掉（见 u3-l6），硬件输出的 `byte_out.txt` 与 Python 的 `data_bytes` 起点一致（都是 MPDU 第一字节），所以字节层面无需再补。

---

### 4.5 调试技巧：--no_sim 与数据格式/位序对齐

#### 4.5.1 概念说明

`test.py` 的两个「调试友好」设计值得单独拎出来讲：一是 `--no_sim` 把「对账」与「仿真」解耦，让你能反复迭代比对逻辑而不必每次等仿真；二是脚本里大量「位序/子载波序对齐」代码，它们不是算法，而是弥合 Python 与 Verilog 两种实现对「同一个量」的表达差异。理解这些对齐，你才能区分「真 bug」与「对齐疏忽」。

#### 4.5.2 核心流程

```
--no_sim 路径:
  跳过 rm sim_out/* / iverilog / vvp
  -> 直接读已有 sim_out/*.txt
  -> 走与正常完全相同的对账逻辑

位序对齐清单 (Python 期望 vs Verilog sim_out):
  SIGNAL  : 每 token 反转 [::-1]              (MSB-first %b vs bit0-first)
  DEMOD   : 末 n_bpsc 位 + 反转               (固定 6 位 %06b vs 有效 n_bpsc 位)
  DEMOD   : 每符号前后半交换                   (负正子载波顺序差异)
  DESCRAM : 硬件侧前补 7 个 0                  (LFSR 直装期不输出)
  BYTE    : 无补偿                             (成字节时已对齐)
```

#### 4.5.3 源码精读

`--no_sim` 的实现就是一个 `if`：

[scripts/test.py:L19-L20](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L19-L20) 定义开关。

[scripts/test.py:L58-L81](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L58-L81) `if not args.no_sim:` 才执行清空与仿真——这就是「对账与仿真解耦」的全部实现，朴素但有效。

> 典型用法：第一次正常跑（带仿真）生成 `sim_out/`；之后你只想调整比对脚本的打印格式或排查某级，加 `--no_sim` 秒级重跑。**前提是 `sim_out/` 没被别的操作清掉**（正常带仿真跑会先 `rm`，所以 `--no_sim` 必须紧接在一次正常仿真之后用）。

位序对齐的几处关键代码（前面 4.4 已逐段引用过，这里做集中索引）：

- SIGNAL token 反转：[scripts/test.py:L84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L84)
- DEMOD 取末 `n_bpsc` 位再反转：[scripts/test.py:L132](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L132)
- DEMOD 子载波前后半交换：[scripts/test.py:L114-L120](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L114-L120)
- DESCRAMBLE 前 7 个 0：[scripts/test.py:L183](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L183)

这些对齐的本质是：**两种实现表达的「同一个物理量」用了不同的位宽、比特方向或子载波顺序**，`test.py` 负责在比对前把它们归一化。改任何一端的表达，都要同步改这里的对齐代码。

#### 4.5.4 代码实践

**实践目标**：亲手制造一次「对齐疏忽导致的假失败」，体会它与真 bug 的区别。

**操作步骤**：

1. 先正常跑一个 legacy 样本，确认各阶段 `works!`。
2. 用编辑器打开 `scripts/test.py`，把 L183 的 `descramble_out = '0'*7 + descramble_out` 临时改成 `descramble_out = '0'*7 + descramble_out` 注释掉（即不补 7 个 0）——**仅用于实验，事后记得改回**（本任务规定不改源码，所以实验后必须还原；或直接复制一份 `test_exp.py` 来改，不动原文件）。
3. 用 `--no_sim` 重跑（复用上一次的 `sim_out`）。
4. 观察 DESCRAMBLE 阶段：Expected 与 Got 会**整体错位 7 个比特**，几乎每个符号都报 `diff`，但内容看起来「差不多」。
5. 改回后重跑，DESCRAMBLE 恢复 `works!`。

**需要观察的现象**：注释掉补偿后，DESCRAMBLE 全符号报错且 diff 数稳定（典型的「整体相位错位」特征）；这与「真 bug」的随机/局部错误模式不同。

**预期结果**：恢复补偿后 DESCRAMBLE 重新 `works!`。借此体会：**看到某级全错、且 diff 模式整齐，先怀疑对齐/补偿，再怀疑算法**。具体输出「待本地验证」。

> 合规提醒：本任务要求不修改源码。若你要做这个实验，请复制 `test.py` 到临时文件再改，或实验后立即还原，不要把改动留在仓库里。

#### 4.5.5 小练习与答案

**练习 1**：`--no_sim` 必须紧接在「一次正常仿真」之后用才安全，为什么？

**参考答案**：因为带仿真的正常运行会先 `rm -rf sim_out/*` 再重新生成；而 `--no_sim` 完全不动 `sim_out/`，只读不写。如果你在 `sim_out/` 被清空（或从没生成）的状态下用 `--no_sim`，脚本会在打开 `signal_out.txt` 时报 `IOError`。所以 `--no_sim` 的隐含前提是：`sim_out/` 里已经有与当前样本匹配的产物。

**练习 2**：假如你在 Verilog 里把 `demod_out` 的位宽从 6 改成了 8（并在 `dot11_tb.v` 里相应改成 `%08b`），`test.py` 还能正确比对吗？要同步改哪里？

**参考答案**：不能直接比对。`test.py` L132 假定每行末尾 `n_bpsc` 位有效且总宽 6（`%06b`），改成 8 位后取末 `n_bpsc` 位的逻辑仍对（因为只取末尾），但若你还改了比特排列顺序则要同步调整反转逻辑。更稳妥的做法是保持测试台格式串与 `test.py` 解析逻辑的成对修改——这也是为什么 `dot11_tb.v` 的 `$fwrite` 格式串与 `test.py` 的解析被视为一份「对账契约」。

---

## 5. 综合实践

把本讲的三段式串起来，做一次完整的「样本 → 对账 → 定位」演练。

**任务**：选一个 conducted 样本（推荐先 legacy 24 Mbps，再进阶到 dot11n 6.5/13 Mbps），完整跑一遍 `test.py`，并产出一页「对账报告」。

**步骤**：

1. **准备**：确认 `scripts/` 下 Python 2 + `scipy` + `wltrace` 就绪；确认 `iverilog`/`vvp` 可用（u1-l2）。
2. **首跑（带仿真）**：
   ```bash
   cd scripts
   python test.py ../testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat
   ```
   记录打印的 `Stop after N samples`，与你在 4.2.4 手算的 `num_sample` 对照。
3. **看对账**：逐阶段记录 `works!` 或 `error`。若全 `works!`，任务完成；若某级失败，按 4.4.4 的「第一失败阶段」法定位。
4. **复跑（不带仿真）**：紧接着用 `--no_sim` 重跑，确认它秒级返回且结果与首跑一致（验证对账与仿真解耦）。
5. **探针验证**（选做，衔接 u5-l3）：对照 4.3.3 的 `$fwrite` 格式串，手动 `cat verilog/sim_out/demod_out.txt | head -3`，确认每行确实是 6 位二进制，与 L206 的 `%06b` 一致。
6. **产出**：写一份不超过 300 字的报告，包含：样本名、`Stop after` 值、各阶段 works/error 状态、若有失败则说明定位到哪一级及初步原因判断。

**验收标准**：

- 能说清 `stop` 是怎么算出来的（量纲正确）。
- 能在日志里准确指出「第一失败阶段」。
- 能解释 `--no_sim` 为什么这次能用（紧接首跑、`sim_out` 未被清空）。

---

## 6. 本讲小结

- `test.py` 是单包端到端对账脚本，三段式编排：**准备（读样本/转 `.txt`）→ 期望（`decode_next`）→ 仿真 + 逐阶段比对**。
- `stop` 由 SIGNAL 的 `length`/`rate` 反推：\(\text{num\_sample}=(\text{length}\times8/\text{rate}+T_\text{pre})\times20\)，作为 `-DNUM_SAMPLE` 传给测试台决定 `$finish` 时机。
- `dot11_tb.v` 用一组 `if (state==S_DECODE_DATA && strobe) $fwrite` 把各级中间结果落盘到 `sim_out/*.txt`，其**格式串就是与 Python 的对账契约**。
- 比对按 SIGNAL → DEMOD → DEINTER → CONV → DESCRAMBLE → BYTE 顺序进行，定位策略是「**第一失败阶段之前的都可信，之后的都不可信**」。
- `test.py` 内含大量「位序/子载波序对齐」代码（token 反转、取末 `n_bpsc` 位、前后半交换、前补 7 个 0），它们弥合两种实现的表达差异，不是算法本身。
- `--no_sim` 把对账与仿真解耦，用于复用 `sim_out` 秒级重跑比对；前提是 `sim_out` 未被清空。

---

## 7. 下一步学习建议

- **深入测试台内部**：本讲把 `dot11_tb.v` 当「被测端」用，它的 `$readmemh`/5:1 节拍/探针落盘细节在 [u5-l3 仿真测试台 dot11_tb.v](u5-l3-testbench.md) 专题展开，建议接着读，并尝试在那里加一个自定义 `$fwrite` 探针。
- **查找表如何来**：`test.py` 依赖的 `bin_to_mem` 之外的脚本（`gen_*_lut.py`、`condense.py`）在 [u5-l4 查找表生成脚本](u5-l4-lut-generators.md) 与 [u5-l5 样本数据处理](u5-l5-sample-data-tooling.md) 讲解。
- **回到 RTL 排查**：当 `test.py` 把 bug 锁定到某一级后，回到对应讲义精读硬件实现——DEMOD 看 [u3-l3](u3-l3-demodulate.md)、DEINTER 看 [u3-l4](u3-l4-deinterleave.md)、CONV 看 [u3-l5](u3-l5-ofdm-decoder-pipeline.md)、SIGNAL/CRC 看单元 4。
- **动手扩展对账**：尝试在 `test.py` 里增加对 `equalizer_out.txt`（星座点）的比对，作为综合实践——这会逼你处理「复数定点（Verilog）vs 复数浮点（Python cons）」的对齐，是检验你是否真懂交叉验证的好题。
