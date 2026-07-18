# 样本数据处理与测试样本集

## 1. 本讲目标

OpenOFDM 的 Verilog 仿真（见前置 u1-l2、u5-l3）不能凭空运行，它需要「真实抓到的 802.11 射频样本」作为输入激励。但 USRP N210 直接抓出来的样本是一段二进制文件，里面既有真正的数据包，也有大段大段的静默（只有噪声、没有信号）。本讲就解决两个工程问题：

1. 如何把 USRP 抓的二进制 `.dat` 样本，转成 Verilog `$readmemh` 能直接吃的文本格式（`bin_to_mem.py`）。
2. 如何把样本里无意义的静默段裁掉，让仿真跑得又快又能对准包起点（`condense.py`）。

学完本讲，你应当能够：

- 说清 `.dat`（二进制 int16 I/Q）到 `.txt`（每行 8 位 hex）的逐字节转换关系，并能手算样本数。
- 解释 `condense.py` 用功率门限 + 滞回窗口裁剪静默的状态机原理。
- 画出「USRP 抓包 → `condense` → `bin_to_mem` → `$readmemh`」的完整数据通路。
- 说出 `testing_inputs/` 下 conducted 与 radiated 两类样本分别覆盖了哪些速率，并发现其中的覆盖缺口。

## 2. 前置知识

阅读本讲前，你应当已经了解（见 u1-l3、u5-l3）：

- **采样约定**：OpenOFDM 工作在 20 MSPS 采样率、100 MHz 时钟下，两者之比正好是 5，所以测试台用 `clk_count==4` 实现「每 5 个时钟喂一个样本」（5:1 节拍）。
- **I/Q 样本格式**：一个 32 位样本里，**高 16 位是 I、低 16 位是 Q**，且都是有符号整数（补码）。
- **`$readmemh`**：Verilog 内建任务，把一个「每行一个 hex 字面量」的文本文件整块灌进一个内存数组 `ram`，是仿真加载样本的标准手段。
- **交叉验证**：u5-l2 的 `test.py` 会用 Python 参考解码器算出「标准答案」，再和 Verilog 仿真输出逐阶段比对——而 Verilog 仿真吃的样本，正是本讲处理的 `.txt` 文件。

两个本讲会用到的 Python 小知识（这两个脚本都是 **Python 2** 写的，与 u5-l1 的 `decode.py` 一致）：

- `ord(b[1])`：Python 2 里对字符串/字节切片得到的是单字符字符串，要取它的整数值必须用 `ord()`。
- `scipy.fromfile(..., dtype=scipy.int16)`：旧版 SciPy 提供的「把二进制文件直接读成 int16 数组」的便捷接口，新版 NumPy 里已无此用法。

## 3. 本讲源码地图

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `scripts/bin_to_mem.py` | 把 USRP 二进制 I/Q 文件转成 `$readmemh` 文本 | 模块 4.1 主角 |
| `scripts/condense.py` | 去除样本中的静默段，压缩仿真时间 | 模块 4.2 主角 |
| `testing_inputs/conducted/readme.txt` | 说明 conducted 样本的采集方式 | 模块 4.3 样本集说明 |
| `verilog/dot11_tb.v` | 仿真测试台，用 `$readmemh` 加载样本 | 串联通路的下游消费者 |
| `scripts/test.py` | 交叉验证脚本，会自动调用 `bin_to_mem` | 串联通路的自动触发点 |
| `docs/source/overview.rst` | 文档对样本集与脚本分工的权威说明 | 样本覆盖与分工的总述 |

## 4. 核心概念与源码讲解

### 4.1 bin_to_mem 样本转换

#### 4.1.1 概念说明

USRP N210 自带的抓包工具 `rx_samples_to_file` 会把接收到的基带 I/Q 样本以**原始二进制**形式落盘成一个 `.dat` 文件：每个样本 4 字节，前 2 字节是 I（int16，小端），后 2 字节是 Q（int16，小端），样本按时间顺序紧密排列，中间没有任何分隔符或文件头。

但 Verilog 的 `$readmemh` 只认**文本格式**——它要求文件每一行是一个十六进制字面量，把这一行的值写进内存数组的一个元素。二进制 `.dat` 它读不了。

`bin_to_mem.py` 就是这两者之间的「翻译器」：把每 4 字节的二进制样本，翻译成一行 8 个 hex 字符的文本（IIIIQQQQ），让 `$readmemh` 能直接吃。

#### 4.1.2 核心流程

`bin_to_mem.py` 的执行过程可以用下面这段伪代码概括：

```
打开 .dat（二进制读）、打开 .txt（文本写，大缓冲）
循环：每次从 .dat 读一个 CHUNK（1 MiB）
  对该 CHUNK 内每 4 字节一组：
    I = 小端解析前 2 字节 → 有符号 short
    Q = 小端解析后 2 字节 → 有符号 short
    I = I / scale      # 默认 scale=1，可整体缩小幅度
    Q = Q / scale
    写出一行：hex4(I) + hex4(Q) + "\n"
  打印进度（已处理字节数、速度、ETA）
```

关键点：

- **小端解析**：低字节在低地址，所以 `低字节 + 高字节<<8`。
- **有符号处理**：int16 的范围是 \([-2^{15},\, 2^{15}-1]\)，超过 \(2^{15}-1=32767\) 的值要减 \(2^{16}\) 还原成负数。
- **hex 编码**：负数先用 `n % 2^{16}` 折回 \([0, 2^{16})\) 的补码无符号值，再格式化成 4 位 hex。

经过转换，一行输出恒为 8 个 hex 字符 + 1 个换行 = **9 字节**，正好对应输入的 4 字节。所以 `.txt` 文件大小 ≈ `.dat` 文件大小的 \(9/4 = 2.25\) 倍（文本可读，但更大）。

#### 4.1.3 源码精读

先看文件头部，作者一句话点明了用途——把 `rx_samples_to_file` 产出的二进制文件转成 `$readmemh` 能读的内存文本：

[scripts/bin_to_mem.py:3-6](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/bin_to_mem.py#L3-L6) —— 说明输入是 `rx_samples_to_file` 生成的二进制文件，输出是 `$readmemh` 可读的内存文本。

核心是小端字节解析函数 `le_bin_str_to_signed_short`：它把 2 个字节按小端拼成 16 位无符号值，再判符号位还原成有符号整数：

[scripts/bin_to_mem.py:17-21](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/bin_to_mem.py#L17-L21) —— `v = ord(b[1])*(1<<8) + ord(b[0])` 是小端拼接；`if v > (1<<15): v = v - (1<<16)` 把超过 32767 的值还原成负数。注意 `ord()` 是 Python 2 写法。

与之配对的是把有符号 short 转成 4 位 hex 字符串：

[scripts/bin_to_mem.py:24-25](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/bin_to_mem.py#L24-L25) —— `format(n%(1<<16), '04x')`：先对 \(2^{16}\) 取模把负数折成补码无符号值，再格式化成至少 4 位的 hex（不足补 0）。

主循环里真正写出一行的就是这一句，**先写 I、后写 Q**，所以一行里前 4 个 hex 是 I、后 4 个 hex 是 Q：

[scripts/bin_to_mem.py:49-53](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/bin_to_mem.py#L49-L53) —— 每 4 字节一组解析出 I、Q（可被 `--scale` 整除缩小），再用 `'%s%s\n'` 拼成 8 位 hex 一行输出。`byte_count += len(bytes)` 与进度打印体现了它是流式分块处理大文件的。

这条「I 在前、Q 在后」的约定，恰好和测试台里 `sample_in[31:16]` 当 I、`sample_in[15:0]` 当 Q 完全对上：`$readmemh` 把一行的 8 位 hex 当成一个 32 位整数写入 `ram`，高 16 位自然落到 I、低 16 位落到 Q。

[verilog/dot11_tb.v:60](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L60) —— `ram` 声明为 32 位宽的内存数组，每个元素装一行 hex（即一个 I/Q 样本）。

[verilog/dot11_tb.v:96](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L96) —— `$readmemh(SAMPLE_FILE, ram)` 把 `bin_to_mem` 生成的 `.txt` 整块灌进 `ram`。

[verilog/dot11_tb.v:150](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L150) 与 [verilog/dot11_tb.v:162](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L162) —— `sample_in <= ram[addr]` 取一个样本；随后 `$signed(sample_in[31:16])` 取 I、`$signed(sample_in[15:0])` 取 Q，正是把 `bin_to_mem` 拼好的 32 位值拆回有符号 I/Q。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 `bin_to_mem`，验证「4 字节二进制 → 1 行 8 位 hex」的对应关系。

**操作步骤**（注意脚本为 Python 2）：

1. 在仓库根目录，挑一个 conducted 样本，复制一份只读副本以免污染原文件（这里仅作演示，实际可直接对原 `.dat` 操作）：

   ```bash
   # 需要 python2；若环境只有 python3，请用 py2 虚拟环境
   python2 scripts/bin_to_mem.py \
     testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat \
     --out /tmp/dot11a_24.txt
   ```

2. 查看输出文件行数与原 `.dat` 字节数：

   ```bash
   wc -l /tmp/dot11a_24.txt
   stat -c %s testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat
   head -3 /tmp/dot11a_24.txt
   ```

**需要观察的现象**：

- `.dat` 字节数除以 4，应当**恰好等于** `.txt` 的行数（每个样本 4 字节 → 1 行）。
- 每行正好 8 个 hex 字符，前 4 位是 I、后 4 位是 Q。
- 仓库自带同名 `.txt` 的行数应与你生成的一致（21440 行）。

**预期结果**（可直接计算，无需运行也可知）：该 `.dat` 为 85760 字节，\(85760 / 4 = 21440\) 个样本，故 `.txt` 应有 21440 行；每行 9 字节（8 hex + 换行），\(21440 \times 9 = 192960\) 字节，与仓库中 `.txt` 实际大小吻合。文件开头的几行值很接近 0（如 `0001ffff` 表示 I=+1、Q=−1），因为包到达前是静默噪声段。**待本地验证**：若你的环境无 `python2`，可改写为 `int.from_bytes(b,'little',signed=True)` 的 Python 3 版本自行验证。

#### 4.1.5 小练习与答案

**练习 1**：`.dat` 里某 4 字节是 `00 01 ff ff`（十六进制字节序），经过 `bin_to_mem` 后会输出哪一行？对应 I、Q 各是多少？

**参考答案**：按小端，I 字节为 `00 01` → \(0x0100 = 256\)；Q 字节为 `ff ff` → \(0xffff = 65535 > 32767\)，减 \(2^{16}\) 得 \(Q = -1\)。输出行为 `0100ffff`，即 I=256、Q=−1。

**练习 2**：为什么 `signed_short_to_hex_str` 里要先做 `n % (1<<16)` 再格式化，而不是直接 `format(n, '04x')`？

**参考答案**：Python 的 `%` 与 `format` 对负数会产出带负号的字符串（如 `-1` → `"-1"`），而 `$readmemh` 需要的是 16 位补码的无符号 hex 字面量。`n % (1<<16)` 把 \(-1\) 折成 \(65535 = \text{0xffff}\)，正好是 −1 的 16 位补码表示，这样输出才是合法的 4 位 hex。

---

### 4.2 condense 静默裁剪

#### 4.2.1 概念说明

USRP 抓的一段样本里，真正有 802.11 包的「爆发段」（burst）往往只占很小一部分，前后大段都是只有底噪的「静默段」。仿真时如果把这些静默段也喂进 `dot11_tb`，会带来两个麻烦：

1. **仿真很慢**：`dot11_tb` 用 `addr == NUM_SAMPLE` 触发 `$finish`，静默段越多、`NUM_SAMPLE` 越大，仿真要跑的样本数就越多，纯属浪费算力。
2. **包起点偏远**：Python 参考解码器定位到的包起始样本号 `begin` 会很大，`test.py` 里 `stop = begin + num_sample + 320` 也跟着变大，整段仿真被拉长。

`condense.py` 的作用就是把这些静默段裁掉，只保留真正的爆发段，从而缩短仿真时间、让包起点更靠近文件头部。

#### 4.2.2 核心流程

`condense.py` 的核心是一个**两态状态机 + 滞回窗口**，按每 80 个样本为一个窗口扫描整段样本：

```
skip：跳过开头的 args.skip 个样本（USRP 上电稳定期，默认 0.1s ≈ 2,000,000 个样本）
state = idle
对每个 80 样本窗口：
  if state == idle:
      if 窗口内任一样本 |I| > thres（默认 1000）:
          state = trigger
          countdown = 800        # 触发后强制再写 800 个样本（滞回）
          packets += 1
  if state == trigger:
      if countdown > 0:           # 滞回期内无条件写
          write 窗口
          countdown -= 80
      elif 窗口内任一样本 |I| > thres:   # 仍有信号，继续写
          write 窗口
      else:                       # 信号消失，回到 idle
          state = idle
```

关键点：

- **只用 I 路绝对值 `abs(c.real)` 当功率代理**：和 `power_trigger.v`（u2-l1）一样，省去平方开方，代价是只看 I 路。门限默认 `thres=1000`，比 `power_trigger` 的 100 大得多，因为这里是离线处理、追求稳定而非实时灵敏度。
- **滞回（hysteresis）**：一旦触发就强制再写 `countdown=800` 个样本（即 10 个窗口），避免包中间短暂凹陷（幅度低谷）被误判为「信号结束」把包腰斩。这与 `power_trigger` 的「连续 N 个低样本才解除」是同一种思想。
- **输出仍是二进制**：`condense` 写出的是 `_condensed.dat`（小端 int16 对），不是文本；之后还要再跑一次 `bin_to_mem` 才能得到 `.txt`。

#### 4.2.3 源码精读

头部说明了它的用途——去除 USRP 原始样本里的 idle 段：

[scripts/condense.py:3-5](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/condense.py#L3-L5) —— 注释点明目标是「removing idle periods」。

三个命令行参数决定了裁剪行为：

[scripts/condense.py:17-23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/condense.py#L17-L23) —— `--thres` 默认 1000（I 路功率门限）、`--skip` 默认 `int(0.1*20e6)=2000000`（跳过 USRP 上电稳定期的 0.1 秒、约 200 万样本）、`--out` 默认在原文件名后插 `_condensed`。

读入样本用了旧版 SciPy 的 `fromfile`，再把 int16 两两配对成复数（注意是 Python 2 的 `print` 语句和 `itertools.izip`）：

[scripts/condense.py:26-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/condense.py#L26-L29) —— `scipy.fromfile(..., dtype=scipy.int16)` 读二进制 int16 数组，`izip(wave[::2], wave[1::2])` 把奇偶位两两配成 (I, Q) 复数样本。

核心状态机就在这段：`idle` 态检测到功率超门限就转 `trigger`，`trigger` 态用 `countdown` 滞回：

[scripts/condense.py:43-61](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/condense.py#L43-L61) —— `idle` 态用 `any([abs(c.real) > args.thres ...])` 扫窗口，命中则 `state='trigger'` 且 `countdown=80*10`（=800）；`trigger` 态下 `countdown>0` 时无条件写、否则继续看门限，两者都不满足才回 `idle`。写入用 `struct.pack('<hh', int(c.real), int(c.imag))` 仍是小端 int16 对，所以输出还是 `.dat` 格式。

末尾打印检出了几个包：

[scripts/condense.py:64-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/condense.py#L64-L65) —— `count` 累计的是「触发次数」，即检出多少个独立的数据包。

#### 4.2.4 代码实践

**实践目标**：对同一个样本做「裁剪前 vs 裁剪后」对比，量化静默段占比与仿真省时效果。

**操作步骤**：

1. 对一个 conducted `.dat` 跑 `condense`（Python 2）：

   ```bash
   python2 scripts/condense.py \
     testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat \
     --out /tmp/dot11a_24_condensed.dat
   ```

2. 比较裁剪前后的样本数（每 4 字节一个样本）：

   ```bash
   echo "原始: $(( $(stat -c %s testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat) / 4 )) 样本"
   echo "裁剪: $(( $(stat -c %s /tmp/dot11a_24_condensed.dat) / 4 )) 样本"
   ```

3. 把裁剪后的 `.dat` 再转成 `.txt` 喂仿真：

   ```bash
   python2 scripts/bin_to_mem.py /tmp/dot11a_24_condensed.dat --out /tmp/dot11a_24_condensed.txt
   wc -l /tmp/dot11a_24_condensed.txt
   ```

**需要观察的现象**：

- `condense` 终端会打印 `N raw samples` 与 `M packets`（conducted 单包样本通常检为 1 个包）。
- 裁剪后的样本数明显小于原始（conducted 样本前后静默不多，差距可能不大；radiated/长抓包差距更显著）。
- 仿真耗时大致正比于 `NUM_SAMPLE`，样本数减少多少倍，仿真就快多少倍。

**预期结果**：仓库自带的 conducted 24Mbps `.dat` 仅 21440 个样本，本身已比较紧凑，裁剪后样本数会进一步下降（具体取决于门限与滞回窗口，**待本地验证**）。真正能体现 `condense` 价值的是「长抓包」（一次抓很多包、含大量静默）的场景——那时裁剪比可达数倍乃至数十倍。注意：`test.py`（u5-l2）**并不会**自动调用 `condense`，它只调用 `bin_to_mem`，所以裁剪是一个需要手动执行的离线步骤。

#### 4.2.5 小练习与答案

**练习 1**：`condense` 的门限默认是 1000，而硬件 `power_trigger.v` 的 `SR_POWER_THRES` 默认只有 100，为什么差距这么大？

**参考答案**：两者度量的「功率代理」不同。`power_trigger` 只取 I 路绝对值，且要在实时、低延迟下灵敏地捕捉微弱包，故门限较低；`condense` 是离线处理，目标是稳健地判别「有没有真实信号」、避免把噪声当包，宁可漏检一些弱信号也不误留噪声段，故门限设得较高。

**练习 2**：如果把 `countdown=80*10` 改成 `countdown=0`（去掉滞回），会出现什么问题？

**参考答案**：包中间一旦出现短暂幅度凹陷（多径衰落或星座瞬时回零），窗口内所有样本 `|I|` 都低于门限，状态机立刻回 `idle` 停止写入，导致包被腰斩、后半段丢失。滞回的 800 个样本「保护区」正是为了跨过这种短暂凹陷。

---

### 4.3 测试样本集

#### 4.3.1 概念说明

`testing_inputs/` 是 OpenOFDM 自带的「测试样本库」，提供覆盖各速率的真实抓包样本，让仿真与交叉验证有料可跑。它分两个子目录：

- **`conducted/`（传导式）**：USRP 与被测设备（TP-LINK WDR3500 AP）的天线口用**同轴线直连**，信号干净、无空间信道干扰，是最稳定的「金样本」，用于回归测试与逐阶段交叉验证。
- **`radiated/`（辐射式 / 空口）**：经过真实无线空间信道（天线发射、空气中传播、天线接收），含多径与衰减，更贴近真实部署，用于压力测试。

这两类的采集方式记录在 `readme.txt`：

[testing_inputs/conducted/readme.txt:1-3](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/testing_inputs/conducted/readme.txt#L1-L3) —— 说明 conducted 样本是把 USRP 与测试设备（TP-LINK WDR3500 AP）的天线口用同轴线直连采得的。

文档对样本集覆盖率的权威说明：

[docs/source/overview.rst:108-110](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/overview.rst#L108-L110) —— 「These files covers all the bit rates (legacy and HT) supported in |project|」，即样本集覆盖项目支持的全部 legacy 与 HT 速率。

#### 4.3.2 核心流程：从抓包到仿真的完整通路

把本讲三个模块串起来，一条完整的「样本数据通路」如下：

```
USRP rx_samples_to_file           # 真实抓包，产出 .dat（小端 int16 I/Q）
        │
        ▼
   (可选) condense.py             # 去静默段 → _condensed.dat（仍是二进制）
        │
        ▼
     bin_to_mem.py                # 二进制 → 文本 hex，每行 IIIIQQQQ
        │
        ▼
      sample.txt                  # $readmemh 可读
        │
        ▼
   dot11_tb.v: $readmemh(...)     # 灌进 ram 数组，按 5:1 节拍喂给 dot11
```

注意 `test.py` 把其中一步自动化了：它发现 `.txt` 不存在或比 `.dat` 旧时，会自动调 `bin_to_mem`，但**不会**调 `condense`：

[scripts/test.py:37-41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L37-L41) —— `memory_file` 取 `<sample>.txt`；若该文件缺失或修改时间早于 `.dat`，则自动调用 `bin_to_mem.py` 重新生成。

随后 `test.py` 按信号字段算出需要的样本数 `stop`，作为 `-DNUM_SAMPLE` 传给测试台，决定 `$finish` 时机：

[scripts/test.py:49-53](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L49-L53) —— `num_sample` 由 SIGNAL 的 length/rate 与前导时长（HT 取 40µs、legacy 取 20µs）换算得出；`stop = begin + num_sample + 320`，`begin` 是 Python 参考解码器定位的包起点。这正是裁掉静默能让 `begin` 变小、`stop` 随之变小、仿真变快的根因。

测试台默认就指向 conducted 24Mbps 样本，并默认跑 3000 个样本：

[verilog/dot11_tb.v:82-87](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L82-L87) —— `SAMPLE_FILE` 默认指向 `conducted/dot11a_24mbps_...txt`、`NUM_SAMPLE` 默认 3000，二者都可用命令行 `-D` 覆盖。

#### 4.3.3 源码精读：样本覆盖盘点

下面这张表把 `testing_inputs/` 下实际存在的样本盘点清楚（依据 `git ls-files testing_inputs/` 的真实文件名）。

| 子目录 | 标准 | 覆盖速率（文件名） | 速率数 |
| --- | --- | --- | --- |
| `conducted/` | 802.11a (legacy) | 6 / 9 / 12 / 18 / 24 / 36 / 48 Mbps | 7 |
| `conducted/` | 802.11n (HT, MCS 0–7) | 6.5 / 7.2 / 13 / 19.5 / 26 / 39 / 52 / 58.5 / 65 Mbps | 9 |
| `radiated/` | 802.11n (HT) | 6.5 / 19.5 / 26 / 65 Mbps | 4 |

几点需要特别留意：

- **legacy 顶速 54 Mbps 缺失**：802.11a 共有 8 个速率（6/9/12/18/24/36/48/54 Mbps），但 conducted 目录里**没有 54 Mbps 样本**。54 Mbps 是 64-QAM 3/4，是 legacy 最高速率。如果你要验证 64-QAM 的 legacy 路径，conducted 里只能用 48 Mbps（64-QAM 2/3）作近似替代，或自行抓一个 54 Mbps 样本补充——这是样本集的一个**覆盖缺口**。
- **HT 覆盖完整**：conducted 的 dot11n 样本正好覆盖 MCS 0–7（6.5/13/19.5/26/39/52/58.5/65 Mbps 一一对应），外加一个 7.2 Mbps（MCS 0 + 短保护间隔 SGI），HT 侧覆盖齐全。
- **radiated 只是子集**：空口样本只有 4 个 HT 速率，没有 legacy，量也少，适合做抽样压力测试而非全速率回归。
- **`.pcap` 是「标准答案」**：部分样本带 `.pcap` 同名文件（如 `dot11n_7.2mbps_...pcap`、radiated 下全部带 `.pcap`），这是用独立工具抓到的原始 802.11 帧的 pcap 包，可作为「地面真值」核对 Verilog 解出的字节是否正确——其作用和 u5-l1 的 Python 参考解码器类似，都是交叉验证的基准。
- **`.txt` 是 `.dat` 的转换产物**：conducted 下大部分样本同时提供 `.dat` 和 `.txt`（仓库维护者已替你跑过 `bin_to_mem`），radiated 只有 19.5 Mbps 预生了 `.txt`，其余需自行转换。

#### 4.3.4 代码实践

**实践目标**：亲手核对样本集覆盖了你关心的所有速率，并发现其中的缺口。

**操作步骤**：

1. 列出 conducted 下所有 legacy 样本名，核对 802.11a 八速率是否齐全：

   ```bash
   ls testing_inputs/conducted/ | grep dot11a | sed 's/_qos_data.*//'
   ```

2. 列出 conducted 下所有 HT 样本名，核对 MCS 0–7 是否齐全：

   ```bash
   ls testing_inputs/conducted/ | grep dot11n | sed 's/_98_.*//'
   ```

3. 挑一个 radiated 样本，自己走完整条转换通路（该目录多数没预生成 `.txt`）：

   ```bash
   python2 scripts/bin_to_mem.py testing_inputs/radiated/dot11n_26mbps.dat \
     --out /tmp/radiated_26.txt
   wc -l /tmp/radiated_26.txt
   ```

**需要观察的现象**：

- 步骤 1 的输出里**找不到 54 Mbps**——确认 legacy 顶速缺口。
- 步骤 2 的输出应包含 MCS 0–7 全部 8 个速率 + 一个 7.2（SGI）。
- 步骤 3 自行生成的 `.txt` 行数 = `.dat` 字节数 ÷ 4。

**预期结果**：conducted legacy 缺 54 Mbps；HT 全覆盖；radiated 仅 4 个 HT 速率。若你的研究关心 54 Mbps 或更多 radiated 场景，需要用 USRP 自行抓包并经 `condense` + `bin_to_mem` 补入。**待本地验证**：步骤 3 的确切行数取决于 `.dat` 大小。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test.py` 会自动调 `bin_to_mem`，却不自动调 `condense`？

**参考答案**：`bin_to_mem` 是仿真「必需」的格式转换（`$readmemh` 只认文本），且可由「`.txt` 比 `.dat` 旧」自动判断要不要重跑，开销小、无副作用。而 `condense` 会改变样本内容（裁掉静默段、移动包起点 `begin`），属于一次性离线预处理，做了之后 `test.py` 里 `begin` 的语义也会变，贸然自动化反而容易出错，所以留给用户手动决定。

**练习 2**：conducted 与 radiated 两类样本，分别更适合用来做什么验证？

**参考答案**：conducted 同轴直连、信道干净，适合做**回归测试与逐阶段交叉验证**（信号无畸变，Python 参考与 Verilog 应严格一致，便于定位 bug）。radiated 含真实空间信道（多径、衰减、噪声），适合做**鲁棒性 / 压力测试**（验证同步、信道估计等模块在非理想信道下是否仍能解出包）。

## 5. 综合实践

把本讲三个模块串成一条端到端的小任务：**自己造一个最小测试样本，并量化裁剪的省时效果**。

任务步骤：

1. **选样本**：挑一个 conducted dot11n 样本（例如 26 Mbps，MCS 3）。
2. **基线**：直接对 `.dat` 跑 `bin_to_mem` 生成 `base.txt`，记录行数 `N_base`。
3. **裁剪**：对同一个 `.dat` 跑 `condense` 得到 `_condensed.dat`，再跑 `bin_to_mem` 得到 `cond.txt`，记录行数 `N_cond`。
4. **分别仿真**：用 `iverilog -DSAMPLE_FILE='"<path>"' -DNUM_SAMPLE=<N> -c verilog/dot11_modules.list verilog/dot11_tb.v -o dot11.out` 与 `vvp -n dot11.out` 跑两次，`NUM_SAMPLE` 分别取 `N_base` 与 `N_cond`（或各自 `N - 1`）。
5. **比对**：

   - 用 `time` 记录两次仿真墙钟耗时，计算加速比 \(N_{base} / N_{cond}\)，验证「耗时 ≈ 正比于样本数」。
   - 检查两次仿真产物 `sim_out/byte_out.txt` 是否一致（裁剪只去静默，不应改变解出的字节内容）。

6. **填一张覆盖表**：把 4.3.3 的样本盘点表抄下来，标出你实际跑通的速率，并注明 54 Mbps 缺口。

**验收标准**：能说出 `.dat → condense → bin_to_mem → $readmemh` 每一步的输入输出格式；能给出裁剪前后的样本数与仿真耗时对比；能指出样本集的覆盖缺口。如果环境无 `python2`，可把两个脚本改写成 Python 3 等价版本（核心是 `int.from_bytes(..., 'little', signed=True)` 与 `numpy.frombuffer(..., dtype=np.int16)`）完成本任务——**待本地验证**。

## 6. 本讲小结

- `bin_to_mem.py` 把 USRP 的二进制 `.dat`（小端 int16 I/Q 对）逐 4 字节翻译成 `$readmemh` 可读的文本，每行 8 位 hex（前 4 位 I、后 4 位 Q），是仿真加载样本的必经转换。
- `condense.py` 用「I 路绝对值门限 + 80 样本窗口 + 800 样本滞回」的两态状态机裁掉静默段，缩短仿真时间、让包起点前移；它输出仍是二进制 `.dat`，需要再跑一次 `bin_to_mem`。
- 完整通路是：USRP 抓包 →（可选 `condense`）→ `bin_to_mem` → `.txt` → `dot11_tb` 的 `$readmemh`；`test.py` 自动做 `bin_to_mem` 但不自动做 `condense`。
- `testing_inputs/conducted` 是同轴直连的干净金样本，覆盖 legacy 6/9/12/18/24/36/48 Mbps 与 HT MCS 0–7（外加 7.2 SGI）；**legacy 顶速 54 Mbps 缺失**是已知覆盖缺口。
- `testing_inputs/radiated` 是空口样本，只覆盖 4 个 HT 速率，适合鲁棒性测试；带 `.pcap` 的样本可作为独立的「地面真值」核对解出的字节。
- 两个脚本均为 **Python 2**（`ord()`、`scipy.fromfile`、`print` 语句、`izip`/`xrange`），与现代 Python 3 环境不直接兼容。

## 7. 下一步学习建议

本讲把「样本从哪来、怎么处理」讲透了，至此第 5 单元（验证与工具链）的数据准备环节闭环。接下来建议：

- **回到验证主链路**：用本讲生成的 `.txt` 样本，配合 u5-l1（Python 参考解码器）与 u5-l2（`test.py` 交叉验证）跑一次完整的逐阶段对账，体会「样本 → 期望 → 仿真 → diff」的闭环。
- **深入测试台探针**：阅读 u5-l3（`dot11_tb.v`），理解 `$readmemh` 之后样本如何按 5:1 节拍喂给 `dot11`、各阶段信号如何落盘——本讲的 `.txt` 正是它的输入。
- **若关心综合与上板**：进入第 6 单元，看这些样本对应的速率如何在 `demodulate.v`、`deinterleave.v` 里被处理，以及真实抓包样本如何最终驱动 USRP N210 上的解码器。
