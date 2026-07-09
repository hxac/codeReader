# 性能、精度与移植实践

## 1. 本讲目标

本讲是「外设、平台与扩展实践」单元的收官篇，也是整本手册把前面所有讲义串起来的「综合迁移课」。读者学完后应该能够：

- 说清楚影响 FREE-TPU V3+ 推理延迟的四个关键因素：IP 频率、DDR 带宽、多核多线程、数据精度。
- 看懂 `setting.ini` 里 `--tpu_threads`、`--int8`、`--sim_data` 等编译参数如何改变生成的 TPU bin。
- 理解 `config.h` 中的魔法地址、`NET_SIZE`/`INPUTDATA_SIZE` 与网络输入分辨率的换算关系。
- 拿到一份「换网络 / 换精度 / 换板卡」的可执行移植 checklist。

本讲依赖 **u3-l1（编译工作流）** 和 **u7-l1（多核多实例）**，并会反复回顾 u1-l3/u1-l4（地址契约）、u4-l4（输入分辨率与打包）、u8-l2（SD 加载）的结论。

## 2. 前置知识

在进入正题前，先用三句话回顾几个贯穿本讲的概念：

- **延迟（latency）**：处理一张输入从「喂进去」到「拿到结果」花费的时间，单位毫秒（ms）。边缘推理追求的就是低延迟。
- **吞吐（throughput）** vs **算力（TOPS/TFLOPS）**：算力是硬件「理论上每秒能做多少次运算」，吞吐是「实际每秒能处理多少张图」，延迟是「单张花多久」。三者相关但不相等——算力高不等于延迟一定低，因为还要看数据搬运。
- **精度（precision）**：每个数值用多少比特表示。FREE-TPU V3+ 免费加密 IP 实际只开放 **FP16**（16 位浮点）与 **INT8**（8 位整数）两种；FP8 属于商用能力，免费版用不了。
- **多核（multi-core）** vs **多线程（multithreading）**：多核指片上有多个独立的 TPU 计算核（如八核）；多线程指单个核内部并发执行多条线程。README 把两者合称「Multi-core with Multithreading technology」。

一句话定位：本讲不是讲新代码，而是讲「**同一个 demo，换几个参数/换一块板子，性能和正确性会怎样变化，我该怎么改**」。

## 3. 本讲源码地图

本讲涉及的关键文件很少，但每一个都是「性能/精度/移植」的旋钮所在：

| 文件 | 作用 | 在本讲中的角色 |
| :--- | :--- | :--- |
| `README.md` | 项目说明与性能表 | 给出官方延迟数据、算力标称、特性清单（性能基准） |
| `sdk/standalone/net_model/scripts/setting.ini` | 编译器参数表 | 精度（`--int8`）与多线程（`--tpu_threads`）的总开关 |
| `sdk/standalone/net_model/scripts/b_yolo4tiny.sh` | 编译执行脚本 | 展示 `setting.ini` 如何被消费、模型路径与 `--extinfo` 在哪改 |
| `sdk/standalone/net_model/scripts/eepbin_cvt.sh` | bin→mem/header 转换 | 移植时必须同步更新的 bin 文件名硬编码 |
| `sdk/standalone/src/config.h` | 裸机编译开关与地址 | 地址契约、`NET_TYPE`、`NET_SIZE`/`INPUTDATA_SIZE` 所在地 |
| `sdk/standalone/src/main.cc` | 裸机主程序 | `NET_TYPE` 如何驱动后处理分支 |
| `sdk/standalone/src/layers/yolo3_detection_output.cpp` | yolo3 软件后处理 | 换检测网络时必须改动的硬编码超参 |

## 4. 核心概念与源码讲解

### 4.1 性能影响因素

#### 4.1.1 概念说明

很多人第一次看 TPU 会以为「算力越高延迟越低」，这只对了一半。README 开宗明义点出真相：

> The performance of Free-TPU V3+ is **highly dependent on the configuration, IP frequency and DDR memory bandwidth**.

也就是说，延迟由四个因素共同决定：

1. **IP 频率**：TPU IP 跑在多高的时钟频率上。频率翻倍，纯计算时间近似减半。
2. **DDR 带宽**：张量数据在 DDR 与 TPU 之间来回搬运的速度。很多网络（尤其是轻量的 MobileNet）是**访存受限（memory-bound）**而非计算受限，此时提频率没用，瓶颈在带宽。
3. **多核多线程**：把一张图拆给多个核/线程并行算。
4. **数据精度**：INT8 比 FP16 数据量减半、算力翻倍，延迟通常更低。

注意一个常见误解：README 给出的延迟表是「**单核 ASIC、1GHz**」理想条件下的数据，**不是你在 FPGA 上实测的数**。FPGA 版的 IP 频率通常远低于 ASIC 的 1GHz，DDR 带宽也受 PL 侧 HP 口限制，所以板上实测延迟会明显高于表中的值。把表当「算力标尺」而非「板上预期」。

#### 4.1.2 核心流程

延迟可以粗略拆成两部分：

\[ T_{\text{latency}} \approx T_{\text{compute}} + T_{\text{memory}} \]

- \(T_{\text{compute}}\)：TPU 真正做乘加的时间，正比于运算量、反比于（频率 × 算力密度）。
- \(T_{\text{memory}}\)：搬权重和特征图的时间，正比于数据量、反比于 DDR 带宽。

精度从 FP16 降到 INT8，对这两部分**同时有利**：算力从 2 TFLOPS 升到 4 TOPS（\(T_{\text{compute}}\) 降），数据量减半（\(T_{\text{memory}}\) 降）。这就是为什么 INT8 延迟几乎总是低于 FP16。

多核并行则受 **Amdahl 定律**约束，无法线性扩展。设网络中可并行部分占比为 \(p\)，用 \(n\) 个核加速的理论上限是：

\[ S(n) = \frac{1}{(1-p) + p/n} \]

即便 \(n\to\infty\)，加速比上限也只有 \(1/(1-p)\)。这意味着串行部分（如不可拆分的层、全局同步、数据搬运）会吃掉多核红利。

#### 4.1.3 源码精读

README 的特性清单把四个因素列得很清楚：

[README.md:L7-L22](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L7-L22) —— 这里列出了 V3+ 的三类特性，其中「Low Latency Computing」一条直接点名 `Dataflow architecture`、`Mixing-precision computing`、`Multi-core with Multithreading technology`，正是本节讨论的精度与多核因素。

[README.md:L37-L48](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L37-L48) —— 这就是官方延迟表，要点有三：

- 第 37 行声明数据条件为「single-core ASIC design with 1GHZ frequency (2TFLOPS/4TOPS)」——单核、1GHz、ASIC。
- MobileNetV2-1.0：FP16 `2.01 ms` → INT8 `1.5 ms`，INT8 约为 FP16 的 75%（没有刚好减半，说明该网络有一部分访存/串行开销不受精度影响）。
- 第 48 行给出多核数据：MobileNetV2 INT8 在「eight-core ASIC」下降到 `0.56 ms`。

用第 48 行的数据套 Amdahl 公式：单核 INT8 是 1.5 ms，八核是 0.56 ms，实测加速比 \(S(8)=1.5/0.56\approx 2.68\)。反推可并行占比 \(p\approx 0.72\)——即该网络约七成可被八核并行吃掉，剩余三成是串行/带宽瓶颈。这与「MobileNet 访存受限」的直觉吻合：层数多、每层计算量小，搬运和同步占比高，多核 scaling 偏差。

#### 4.1.4 代码实践

**实践目标**：用 README 的公开数据，定量感受「精度」与「多核」两条优化路线的收益边界。

**操作步骤**：

1. 读 [README.md:L39-L46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L39-L46) 的三组网络（MobileNetV2、Resnet50、Yolov5s）。
2. 对每个网络计算 INT8 相对 FP16 的延迟比 \(r = T_{\text{INT8}}/T_{\text{FP16}}\)。
3. 观察 \(r\) 是否随网络变大而变化，并尝试解释。

**需要观察的现象**：三个网络的 \(r\) 分别约为 \(1.5/2.01\approx0.75\)、\(6.19/9.52\approx0.65\)、\(14.19/21.82\approx0.65\)。

**预期结果**：网络越大、计算越密集（Resnet50、Yolov5s），INT8 收益越接近「算力翻倍」的理论 0.5；网络越小越访存受限（MobileNetV2），INT8 收益被带宽吃掉一部分，\(r\) 偏高。这说明「换 INT8 能省多少」取决于网络本身是计算受限还是访存受限。

**待本地验证**：以上是基于 README 公开数据的纸面分析；在你自己的板子上换 INT8 后的实际收益，需用 `EEP_DEBUG_INFO` 下的 `EEPTPU_RUNTIMER_REG`（见 4.3 节）实测。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 强调性能「highly dependent on DDR memory bandwidth」，而不是只强调算力？

**答案**：因为边缘推理网络（尤其 MobileNet 这类轻量网络）往往是访存受限的：每层计算量小，但要把权重和中间特征图反复搬进搬出 TPU。此时算力再高也用不满，瓶颈变成 DDR 带宽，所以延迟强依赖带宽。

**练习 2**：八核 MobileNetV2 INT8 的加速比只有约 2.68 倍，远不是 8 倍。请用 Amdahl 定律解释，并指出是哪类开销「拖了后腿」。

**答案**：Amdahl 定律 \(S=1/((1-p)+p/n)\)，当 \(n=8\)、\(S=2.68\) 时反解得 \(p\approx0.72\)，即约 28% 的工作量无法被八核并行化。这部分主要是数据搬运（DDR 带宽）、层间同步以及不可拆分的串行算子，它们不会随核数增加而缩短，因此吃掉了多核红利。

---

### 4.2 多线程与量化参数

#### 4.2.1 概念说明

精度（FP16/INT8）和多线程（`--tpu_threads`）**不是运行时改的，而是编译时烤进 bin 的**。这是 u3-l1 的核心结论，本节再聚焦到这两个旋钮上。

- **量化（quantization）**：把 FP32/FP16 的权重和激活压缩成 INT8。`eeptpu_compiler` 用 `--int8` 开关触发，需要校准数据（`--sim_data`/`--image`）来统计激活范围。
- **多线程（`--tpu_threads N`）**：告诉编译器把网络调度成 \(N\) 条线程并发执行的形式。线程数越多，对单核内部的并发挖掘越深，但也需要校准数据来评估调度。

`setting.ini` 把这些参数以「方案（scheme）」的形式预置好，靠注释切换——这是这个项目最朴素的「配置管理」方式。

#### 4.2.2 核心流程

换精度/换线程数的流程是：

1. 在 `setting.ini` 里注释掉当前方案、启用目标方案的 `global_cmd` 与 `bin_name` 两行。
2. 跑 `b_yolo4tiny.sh` 重新编译，产出新的 `*.pub.bin`。
3. 跑 `eepbin_cvt.sh` 把新 bin 转成 `eepnet.h`/`eepnet.mem`/`eepinput.mem`。
4. 把新产物拷进裸机工程，重新编译 ELF。

关键认知：**`bin_name` 是贯穿编译→转换→加载三步的字符串契约**（u3-l2 已强调）。你换了精度或线程数，`bin_name` 必然要改（例如从 `eeptpu_s2.pub.bin` 改成 `nntpu_int8.pub.bin`），那么 `eepbin_cvt.sh` 里硬编码的 bin 路径也必须同步改，否则转换的是旧 bin。

#### 4.2.3 源码精读

[setting.ini:L8-L10](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L8-L10) —— 当前启用的默认方案 `s2+sim`：FP16、默认线程、带 `--sim_data`，产物 `eeptpu_s2.pub.bin`。注意四个 `--base_*` 地址定义了张量在 DDR 的布局（见 4.3 节）。

[setting.ini:L12-L14](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L12-L14) —— 这就是「INT8 方案」`s2quant+sim`：在默认方案基础上加了 `--int8`，产物改名为 `nntpu_int8.pub.bin`。想跑 INT8，启用这三行即可。

[setting.ini:L16-L22](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L16-L22) —— `s2t4+sim` 和 `s2t4quant+sim` 加了 `--tpu_threads 4`，演示四线程；[setting.ini:L25-L31](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L25-L31) 则是 `--tpu_threads 2` 的两线程方案。可以看出，精度（`--int8`）与线程数（`--tpu_threads N`）是两个**正交**的旋钮，可以自由组合。

`setting.ini` 的内容是怎么被消费的？看脚本里的迷你 ini 解析器：

[b_yolo4tiny.sh:L33-L45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L33-L45) —— 用一行 `awk` 读出 `compiler/model_root/global_cmd/bin_name` 四个键。注意它读的是 `global_cmd` **这一行的字面内容**，所以你切换方案时，真正生效的是哪一行没被注释，而不是方案标签名。

[b_yolo4tiny.sh:L90-L100](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L90-L100) —— 最终拼出的编译命令。注意三点：①`cfg`/`wts` 路径**硬编码**在脚本里（不是 setting.ini），换网络模型要改这里；②`--mean`/`--norm` 系数（`norm=0.003921569≈1/255`）也在这里，会被烤进 bin；③`--extinfo` 的类别表在这里，换数据集要改这里。

而转换脚本里 bin 名是写死的：

[eepbin_cvt.sh:L3-L8](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3-L8) —— 两次调用 `eepBinCvt`，`--bin` 都硬编码为 `eeptpu_s2.pub.bin`。一旦你在 `setting.ini` 里把 `bin_name` 改成了 `nntpu_int8.pub.bin`，**这两处必须同步改**，否则转换的还是旧的 FP16 bin，INT8 白配。

#### 4.2.4 代码实践

**实践目标**：亲手把 yolov4-tiny 从 FP16 切到 INT8，并验证「bin 名契约」是否被尊重。

**操作步骤**：

1. 打开 [setting.ini:L8-L14](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L8-L14)，注释掉第 9–10 行（`s2+sim`），取消注释第 13–14 行（`s2quant+sim`）。
2. 检查 [eepbin_cvt.sh:L3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3) 与第 8 行的 `--bin` 路径——它们仍指向 `eeptpu_s2.pub.bin`，**故意先不改**。
3. 跑 `bash b_yolo4tiny.sh`（需要 x86 主机上有 `eeptpu_compiler`），观察产出的 bin 名。
4. 跑 `bash eepbin_cvt.sh`，观察它转换的是哪个 bin。

**需要观察的现象**：第 3 步产出的新 bin 叫 `nntpu_int8.pub.bin`；第 4 步转换脚本因为 `--bin` 还写着旧名，要么报「文件不存在」，要么转换的还是旧 FP16 bin。

**预期结果**：你会直观撞上「bin 名契约」——`setting.ini` 改了名，下游 `eepbin_cvt.sh` 没跟上，链路断裂。修复方法：把 `eepbin_cvt.sh` 两处 `--bin` 改成 `nntpu_int8.pub.bin`。

**待本地验证**：本实践依赖 x86 主机上的编译器与转换工具，若环境不具备，可只做「纸面推演」——逐行标注哪些文件引用了 `bin_name`，确认改名波及面。

#### 4.2.5 小练习与答案

**练习 1**：`--tpu_threads 4` 和「八核」是一回事吗？

**答案**：不是。`--tpu_threads N` 是**单核内部**的并发线程数，由编译器调度；「八核」是片上有 8 个独立 TPU 核（README 第 48 行）。两者都属于「Multi-core with Multithreading」特性，但作用层次不同，且 `--tpu_threads` 是编译参数、核数是硬件/IP 配置。

**练习 2**：为什么 INT8 方案都带着 `--sim_data`？

**答案**：INT8 量化需要校准——用样例数据统计每一层激活值的分布范围，才能确定量化比例。`--sim_data` 让编译器跑仿真数据来做这个校准。FP16 不需要量化，所以默认方案带不带 `--sim_data` 影响较小；INT8 则强烈依赖它。

---

### 4.3 地址与分辨率移植

#### 4.3.1 概念说明

「移植」有两层：换网络（改模型/精度/分辨率）和换板卡（改地址映射）。本节先讲后者的核心——**地址契约**，以及前者最容易算错的——**分辨率与数据尺寸的换算**。

回顾 u1-l3/u1-l4 的核心结论：裸机代码里那些「魔法地址」不是随便写的，而是被 Vivado 工程的 `assign_bd_address` 定死的软硬件契约。移植到不同板卡或不同地址映射时，这些地址必须与硬件设计保持一致。

分辨率则通过 u4-l4 讲过的「16 通道分组、32 字节步长」打包格式，直接决定了输入数据占多少字节——也就是 `config.h` 里的 `INPUTDATA_SIZE`。

#### 4.3.2 核心流程

地址契约分两条通路（与 u4-l2/u4-l3 一致）：

- **控制通路**：CPU 经 AXI 写 TPU 寄存器，落在 `EEPTPU_REG_BASE_ADDR = 0xA0000000`（arm64/ZynqMP）。
- **数据通路**：TPU 经 HP 口访问 DDR 上的张量，张量区起于 `EEPTPU_MEM_BASE_ADDR = 0x31000000`。

数据区地址必须落在 DDR 低 2GB 内（u1-l4 已说明高 4GB 被排除），否则 TPU 的 HP0 口够不到。

分辨率换算：网络输入分辨率 \(W\times H\)、通道数 \(C\)，按 TPU 的「16 通道分组、每通道 2 字节、32 字节步长」打包后，输入字节数为：

\[ \text{INPUTDATA\_SIZE} = W \times H \times \lceil C/16 \rceil_{16} \times 32 \]

其中 \(\lceil C/16 \rceil_{16}\) 是把通道数向上取整到 16 的倍数（通道不足 16 也占满一个 32 字节槽位）。对 \(C=3\)，取整后为 16，所以每个空间位置固定 32 字节。

#### 4.3.3 源码精读

[config.h:L25-L27](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L25-L27) —— 三个基地址：`EEPTPU_MEM_BASE_ADDR=0x31000000`（数据区）、`EEPTPU_REG_BASE_ADDR=0xA0000000`（TPU 寄存器）、`EEPDVP_REG_BASE_ADDR=0xA00C0000`（摄像头寄存器）。换板卡时，这三处必须与新工程的 `assign_bd_address` 对齐。

[config.h:L28-L35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L28-L35) —— 寄存器偏移定义：`BASEADDR0~3`（0x50/54/58/5C，对应 par/in/tmp/out 四段）、`ALGOADDR`(0x30)、`STARTUP`(0x34)、`STATUS`(0x0C)、`RUNTIMER`(0x24)。这些是 TPU IP 的寄存器协议，换 IP 版本时可能变，换板卡通常不变。

[config.h:L40-L47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L40-L47) —— 移植最常动的几行：`NET_TYPE`（网络类型枚举）、`NET_SIZE=12240064`（权重 `eepnet.mem` 的字节数，约 12 MB）、`INPUTDATA_SIZE=5537792`（输入 `eepinput.mem` 的字节数）。

用上面公式验算 `INPUTDATA_SIZE`：yolov4-tiny 输入 \(416\times416\times3\)，\(C=3\) 取整到 16：

\[ 416\times416\times32 = 173056\times32 = 5{,}537{,}792 \]

与 `config.h` 第 47 行的 `5537792` **逐字节吻合**。这就是分辨率→尺寸的换算实证（u4-l4 也给过同一结论）。

注意编译器侧的地址：[setting.ini:L9](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L9) 里 `--base_par/in/out` 都是 `0x30000000`、`--base_tmp=0x80000000`。这与 `config.h` 的 `EEPTPU_MEM_BASE_ADDR=0x31000000` 数值不同——因为编译器侧的 `--base_*` 是 bin 内部地址表的逻辑锚点，运行时 `eepnet_config[]` 存的是相对偏移 `ofs`，由 `eeptpu_init` 加上 `mem_base` 才得到绝对地址（u3-l3 已讲）。移植时，要保证「编译器 base + 偏移」最终落进 `0x31000000` 起的、HP0 可达的 DDR 区域。

#### 4.3.4 代码实践

**实践目标**：把网络输入分辨率从 416×416 换成 320×320，手算新的 `INPUTDATA_SIZE`，并定位需要同步改的所有位置。

**操作步骤**：

1. 用公式算：\(320\times320\times32 = 102400\times32 = 3{,}276{,}800\) 字节。
2. 在 [config.h:L47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L47) 把 `INPUTDATA_SIZE` 改成 `3276800`。
3. 回顾 u8-l2：`file_read` 用 `INPUTDATA_SIZE` 作长度从 SD 卡读 `eepinput.mem`，所以 SD 卡上的 `eepinput.mem` 也必须是用新分辨率重新转换生成的（重跑 `eepbin_cvt.sh`）。

**需要观察的现象**：如果只改了 `INPUTDATA_SIZE` 却没重新生成 `eepinput.mem`，`file_read` 会按新长度读旧文件，要么读不全、要么读到越界字节。

**预期结果**：`INPUTDATA_SIZE` 与磁盘上 `eepinput.mem` 的实际大小必须逐字节相等（u8-l2 的硬约束）。尺寸算错会导致输入数据错位、推理结果全乱。

**待本地验证**：实际分辨率取决于你的网络 cfg；若你用的是别的输入尺寸，按 \(W\times H\times32\) 重算即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `EEPTPU_MEM_BASE_ADDR=0x31000000` 而不能放到比如 `0xC0000000`？

**答案**：因为 TPU 的数据通路走 PL 侧的 HP0 口访问 DDR，而该工程的地址映射只把 DDR 低 2GB 暴露给 HP0（高 4GB 被排除，见 u1-l4）。`0x31000000` 在低 2GB 内、HP0 可达；放到 `0xC0000000` 则 TPU 够不到，读写无效。

**练习 2**：若把输入通道数从 3 改成 1（灰度图），`INPUTDATA_SIZE` 会变吗？

**答案**：不会。因为 TPU 按 16 通道分组，\(C=1\) 向上取整仍是 16，每个空间位置仍占 32 字节。真实通道外的字节由 `memset` 清零（u4-l4）。只有当真实通道跨过 16 的倍数边界时，尺寸才会变。

---

### 4.4 移植 checklist

#### 4.4.1 概念说明

前三个模块讲了「为什么」和「单点怎么改」，本节把它们组织成一份可照着走的 **checklist**。移植分两大场景：

- **换网络/换精度**：模型变了（如 yolov4-tiny → mobilenet-ssd），或精度变了（FP16 → INT8）。动的是编译侧 + 后处理 + 尺寸常量。
- **换板卡/换地址映射**：硬件平台变了。动的是 `config.h` 地址 + Vivado 工程。

两者的共同点是：**改动呈链式传播**，漏掉一环就会得到「能编译、能跑、但结果错」的最难调的 bug。

#### 4.4.2 核心流程

换网络的链式传播（伪代码）：

```
改模型 cfg/weights  →  改 setting.ini(--int8/--tpu_threads/bin_name)
                  →  改 eepbin_cvt.sh(--bin 名)
                  →  重新编译生成 *.pub.bin
                  →  重新转换生成 eepnet.h / eepnet.mem / eepinput.mem
                  →  改 config.h(NET_TYPE / NET_SIZE / INPUTDATA_SIZE)
                  →  改后处理(num_class / anchors / 或换 SSD 后处理)
                  →  重新编译裸机 ELF
```

换板卡的链式传播：

```
改 Vivado 工程地址映射(assign_bd_address)
  →  改 config.h(EEPTPU_MEM_BASE_ADDR / EEPTPU_REG_BASE_ADDR / EEPDVP_REG_BASE_ADDR)
  →  确认新地址在 HP0 可达的低 2GB 内
  →  重新生成 BOOT.BIN / xsa
```

#### 4.4.3 源码精读

`NET_TYPE` 如何驱动后处理分支：

[main.cc:L400-L418](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L400-L418) —— `NET_TYPE == NetType_Classify` 时走 `get_topk`（分类 topk）；否则走 `yolo3_detection_output_forward`（检测解码+NMS）。所以换网络类型时，这段条件编译决定了走哪条后处理。

[config.h:L50-L52](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L50-L52) —— `YOLO3_DETECTION_OUTPUT` 仅当 `NET_TYPE == NetType_Object_Detect` 时置 1，注释明确「only for yolo3」。**这是移植时最容易踩的坑**：如果你换的是非 yolo3 的检测网络（如 SSD），这个开关和对应的软件后处理并不适用。

换检测网络时必须改的硬编码超参：

[yolo3_detection_output.cpp:L82-L121](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp#L82-L121) —— `num_class=80`、`num_box=3`、`confidence_threshold=0.25`、`nms_threshold=0.5`、12 个 `biases`（anchor）、`mask`、`anchors_scale` 全是**针对当前 yolo 网络**写死的。换数据集（如 COCO→VOC）要改 `num_class` 和 `--extinfo` 类别表；换网络结构要改 anchor/mask/scale。这三方（网络 cfg、编译时 `--extinfo`、运行时这些超参）必须一致，否则检测框全错。

后处理目录里实际只提供了两种算子：

[post_process.h:L24](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/post_process.h#L24) —— 只声明了 `get_topk`；`yolo3_detection_output_forward` 是在 `main.cc` 里 `extern` 引入的（[main.cc:L246](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L246)）。也就是说，**仓库没有现成的 SSD 后处理**——这是移植到 mobilenet-ssd 时必须自行补上的一环。

#### 4.4.4 代码实践（综合 checklist 落地）

**实践目标**：把下面的「综合实践」任务拆成一张可勾选的 checklist，并标注每一项对应改哪个文件、哪一行附近。

**操作步骤**：对照本讲源码，整理出下表（答案见第 5 节综合实践）。

**需要观察的现象**：你会发现改动涉及 5 个文件、横跨编译侧与裸机侧，且后处理是最大不确定项。

**预期结果**：形成一张「文件—改动点—风险」三列表，作为以后任何移植的模板。

#### 4.4.5 小练习与答案

**练习 1**：移植时，为什么「能编译、能跑、但检测结果全错」比「编译失败」更危险？

**答案**：编译失败会立刻暴露问题所在；而「能跑但结果错」往往是因为某一环没同步（如 `num_class` 与 `--extinfo` 不一致、或用了旧 bin），程序不报错却悄悄输出错误框。这类 bug 难以定位，所以 checklist 强调「链式同步、逐环核对」。

**练习 2**：换板卡时，`config.h` 的基地址改了，但 Vivado 工程没改，会发生什么？

**答案**：软件写到新地址的寄存器/数据，硬件并没有把该地址映射到 TPU，于是写入落空或写到无关内存，TPU 收不到命令、读到的是垃圾数据。表现通常是 forward 卡死（轮询 STATUS 永不完成）或输出全错。地址是软硬件契约，两边必须同步。

---

## 5. 综合实践

**任务**：把 demo 从 **yolov4-tiny（FP16）** 换成 **mobilenet-ssd** 并改跑 **INT8**。请写出需要修改的 `setting.ini` 参数、需要重新生成的 bin/mem，以及 `config.h` 中 `NET_TYPE` 该如何设置。

下面是完整的移植方案（结合真实源码，逐项标注）。

### 第 1 步：`setting.ini`（精度与产物名）

打开 [setting.ini:L8-L14](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L8-L14)：

- 注释掉第 9–10 行的 `s2+sim`（FP16）。
- 启用第 13–14 行的 `s2quant+sim`，即 `global_cmd` 带 `--int8`，`bin_name=nntpu_int8.pub.bin`。
- 四个 `--base_*` 地址**保持不变**（地址契约与网络无关）。

> 注意：`setting.ini` 只管「编什么精度/线程、叫什么名」，**模型路径不在 ini 里**。

### 第 2 步：`b_yolo4tiny.sh`（模型路径与类别表）

打开 [b_yolo4tiny.sh:L90-L97](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L90-L97)：

- 第 90–91 行的 `cfg`/`wts` 从 `yolov4_tiny.cfg/.weights` 改成 mobilenet-ssd 的 cfg/weights（darknet 格式）。
- 第 92 行 `img_ssd` 校准图可保留。
- 第 95 行 `--extinfo` 的类别表按 mobilenet-ssd 的数据集改（SSD 常用 VOC 20 类，需与后处理 `num_class` 一致）。
- `--mean`/`--norm` 按 mobilenet-ssd 的预处理要求核对（SSD 通常非零 mean）。

### 第 3 步：`eepbin_cvt.sh`（同步 bin 名契约）

打开 [eepbin_cvt.sh:L3-L8](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3-L8)：把两处 `--bin ./scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin` 改成 `.../nntpu_int8.pub.bin`（与第 1 步的 `bin_name` 一致）。

### 第 4 步：重新生成产物

按顺序跑：

1. `bash b_yolo4tiny.sh` → 产出 `nntpu_int8.pub.bin`（INT8 的 mobilenet-ssd bin）。
2. `bash eepbin_cvt.sh` → 产出三件套：
   - `eepnet.h`（含新的输入输出 shape、`eepnet_config[]` 元数据）
   - `eepnet.mem`（新权重，**新的 `NET_SIZE`**）
   - `eepinput.mem`（新分辨率下的输入，**新的 `INPUTDATA_SIZE`**）

### 第 5 步：`config.h`

打开 [config.h:L40-L52](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L40-L52)：

- **`NET_TYPE`**：mobilenet-ssd 是目标检测网络，按第 41 行注释 `NetType_Object_Detect 1 // e.g. mobilenet-ssd, mobilenet-yolo`，**保持 `NetType_Object_Detect` 不变**。
- **`NET_SIZE`**：改成新 `eepnet.mem` 的实际字节数（重新转换后看文件大小）。
- **`INPUTDATA_SIZE`**：按新输入分辨率 \(W\times H\times32\) 重算（见 4.3 节公式）。
- **`YOLO3_DETECTION_OUTPUT`（第 51 行）**：**这是关键不确定项**。该开关注释明确「only for yolo3」，mobilenet-ssd 不是 yolo3，其检测头输出格式不同，[yolo3_detection_output.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/layers/yolo3_detection_output.cpp) 的解码逻辑（anchor/mask）不适用。仓库未提供 SSD 后处理（[post_process.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/post_process.h#L24) 仅有 `get_topk`），**需自行实现一个 SSD detection-output 软件层**并替换 [main.cc:L411-L418](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L411-L418) 的调用。此项标为「**待本地实现/待确认**」。

### 移植 checklist 汇总表

| 步骤 | 文件 | 改动点 | 风险 |
| :--- | :--- | :--- | :--- |
| 精度 | `setting.ini` | 启用 `--int8` 方案、改 `bin_name` | 漏改 `bin_name` |
| 模型 | `b_yolo4tiny.sh` | `cfg`/`wts` 路径、`--extinfo` 类别表 | 类别表与后处理不一致 |
| 契约 | `eepbin_cvt.sh` | 同步两处 `--bin` 名 | 转换了旧 bin |
| 产物 | （工具产出） | 重生成 `eepnet.h/.mem`、`eepinput.mem` | 用了旧 mem |
| 类型 | `config.h` | `NET_TYPE` 保持 Object_Detect | 误改成 Classify |
| 尺寸 | `config.h` | 重算 `NET_SIZE`/`INPUTDATA_SIZE` | 与磁盘文件不符 |
| 后处理 | `yolo3_detection_output.cpp`/新文件 | SSD 需新后处理层（仓库未提供） | **最大不确定项** |

## 6. 本讲小结

- 影响延迟的四因素：**IP 频率、DDR 带宽、多核多线程、精度**；README 的延迟表是单核 1GHz ASIC 理想值，非板上实测。
- **INT8 比 FP16 算力翻倍且数据量减半**，延迟更低；但访存受限网络（MobileNet）收益打折，计算密集网络（Resnet/Yolo）收益更接近理论值。
- 多核加速受 **Amdahl 定律**约束：八核 MobileNetV2 INT8 实测仅约 2.68 倍，说明约三成工作是串行/带宽瓶颈。
- 精度（`--int8`）与线程（`--tpu_threads N`）是**编译时烤进 bin 的正交旋钮**，在 `setting.ini` 里靠注释切方案；`bin_name` 是贯穿编译→转换→加载的字符串契约。
- 地址是软硬件契约：`config.h` 的 `EEPTPU_MEM_BASE_ADDR=0x31000000` 等必须与 Vivado 的 `assign_bd_address` 对齐，且数据区须在 HP0 可达的低 2GB 内。
- `INPUTDATA_SIZE = W×H×32`（16 通道分组、32 字节步长），yolov4-tiny 的 416×416×3 恰好等于 `5537792`，与 `config.h` 逐字节吻合。
- 移植呈**链式传播**：换网络要同步改 `setting.ini`→`eepbin_cvt.sh`→重生成 mem→`config.h`→后处理；漏一环即「能跑但结果错」。

## 7. 下一步学习建议

本讲是手册的收官综合课。完成本讲后，建议读者：

1. **动手做一次真实移植**：找一个新的小网络（如一个分类网络），按本讲 checklist 走一遍「cfg→编译→转换→改 config.h→跑通」，这是把全册知识内化的最快路径。
2. **回到编译链路深挖**：若对 `--int8` 量化如何影响精度感兴趣，重读 u3-l1/u3-l2/u3-l3，关注 `eepnet_config[]` 里的 `exp` 定点指数如何随精度变化。
3. **关注商用能力边界**：本册基于免费版（仅 FP16/INT8、有一小时限制）。若项目提到下一代支持 Transformer、FP8，那将是免费版之外的新领域，可关注仓库更新。
4. **横向对照 Linux 路线**：本讲的移植以裸机为主线；可对照 u2-l3/u2-l4 的 Linux demo，体会「换网络在 Linux 路线只需换 bin、后处理改起来更轻」的差异，理解两条路线的工程取舍。
