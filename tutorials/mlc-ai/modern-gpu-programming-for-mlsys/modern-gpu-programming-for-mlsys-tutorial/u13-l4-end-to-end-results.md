# 端到端性能解读：从 70ms 到对齐 cuBLAS

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `chapter_gemm_advanced` 末尾的九步性能表：知道每个数字是在什么条件下测出来的、哪些行可以直接比较、哪些行不能。
2. 把 70 ms → 0.094 ms（约 744×）的总增益拆成书定义的四个比较区间，并对每一段增益做出有源码依据的机制归因，而不是笼统地说"用了 TMA 所以快了"。
3. 用书中数据独立复算累计加速比与 TFLOPS，并用 Python 重绘性能曲线（可直接改编仓库自带的 `img/scripts/gen_gemm_perf.py`）。
4. 按附录基准协议列出复现这套实验必须控制的变量清单，理解"同条件下比较版本"与"代表峰值性能"是两个不同的主张。

本讲不引入新的内核机制——九个版本各自的原理已在 u11–u13 前三讲讲完。本讲做的是把机制翻译成数字、再把数字还原回机制的"收官动作"。

## 2. 前置知识

本讲默认你已完成以下认知（均来自前置讲义），这里只做要点回顾：

- **九步优化路线**（u11-l1、u3-l3）：Step 1–3 搭正确性骨架（单 tile、K 循环、空间分块），Step 4–6 引入 TMA、双缓冲软件流水线、持久内核，Step 7–9 做 warp 特化、双 CTA cluster、多消费者。按优化阶梯看，前四步约 142×，后五步约 5×。
- **算术强度与 roofline**（u3-l1、u3-l2）：AI = FLOP/byte，分母绑定具体内存层级；分块复用把 tile 级 AI 抬到约 B/s。本讲会用 AI 定量解释 Step 8/9 的增益。
- **三角色流水线**（u13-l1）：Step 7 把 TMA 生产者、MMA 消费者、回写 warpgroup 拆给并发角色，四道 full/empty 屏障交接缓冲所有权。
- **双 CTA cluster 与多消费者**（u13-l2、u13-l3）：Step 8 用 `cta_group=2` 协作 MMA 产出 256×256 输出 tile、经 DSMEM 读对端 B 切片；Step 9 加第二个 MMA 消费者共享同一 staged B。
- **TFLOPS 换算**（u11-l1）：吞吐 = 2MNK / t，本讲会用到附录给出的精确公式。
- 术语提醒：**staged 操作数**指已由 TMA 装入 SMEM、等待 MMA 消费的 tile；**累计加速比**指相对 Step 1 基线的倍数，**区间加速比**指相邻两个实测版本之间的倍数。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `chapter_gemm_advanced/index.md` | 本讲主源码。文件末尾 "End-to-End Results" 一节给出九步性能表、测量条件、可比性边界与四个比较区间（本章前部还含 Step 7/8/9 三个完整内核，是归因的机制依据） |
| `img/scripts/gen_gemm_perf.py` | 生成书中性能图 `img/gemm_perf.png` 的 matplotlib 脚本，内含六个实测数据点的精确耗时 |
| `img/scripts/README.md` | 说明图表脚本的运行方式与依赖（`matplotlib`、`numpy`） |
| `appendix/benchmarking_gpu_kernels.md` | 基准测试协议：正确性先行、计时边界、CUDA events、`tvm.tirx.bench`、条件一致性与吞吐换算，是本讲"基准条件"模块的依据 |
| `img/gemm_perf.png` | 脚本的输出图，正文以 `../img/gemm_perf.png` 引用 |

## 4. 核心概念与源码讲解

### 4.1 模块一：九步性能表

#### 4.1.1 概念说明

九步性能表是全书 GEMM 主线的"成绩单"：它把九个版本的内核放在同一块 B200、同一个问题规模下计时，并附上 cuBLAS 作为参考实现。这张表回答两个问题——每步优化值得多少毫秒，以及教学内核离工业库还有多远。

但一张性能表只有在测量条件明确时才有意义。书在给出表格前先声明了四个条件：NVIDIA B200、`M=N=K=4096`、fp16 输入、锁定时钟（locked clocks）、每个被测版本 1000 次计时迭代。这五个要素（硬件、问题规模、数据类型、时钟策略、迭代预算）共同构成表格的可比性基础——缺了任何一个，数字之间的除法就不再成立。

表里还有一个容易误读的细节：Step 2、5、6 三行的 Time 与 Speedup 都是"—"。这不是漏测，而是刻意的可比性设计，我们在 4.1.2 展开。

#### 4.1.2 核心流程

一张行可比的性能表，生成流程是：

1. **固定协议**：所有版本用同一 GPU、同一问题规模（`M=N=K=4096`，fp16）、同一时钟策略与迭代预算计时。
2. **决定哪些行入表**：只给能与全矩阵结果直接比较的版本填数；机制被后续版本完整包含的中间版本（Step 5、6）和只算单 tile 的版本（Step 2）用破折号。
3. **计算累计加速比**：以 Step 1 为 1× 基线，每行 speedup = t(Step 1) / t(该行)。
4. **绘图**：脚本 `gen_gemm_perf.py` 只画有实测时间的六个数据点（Step 3、4、7、8、9、cuBLAS），纵轴取对数。

关于第 4 步为什么用对数轴：数据从 53.6 ms 跨到 0.094 ms，相差约 570 倍（近 3 个数量级）。线性轴下后四根柱子会矮到不可见，对数轴才能让每个版本的相对差异都可见。

#### 4.1.3 源码精读

先看测量条件与表格本体：

> [chapter_gemm_advanced/index.md:862-877](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L877)

这段先声明测量在 NVIDIA B200、`M=N=K=4096`、fp16 输入、锁定时钟、每版本 1000 次计时迭代下进行，并要求新的测量与复现遵循 `chap_benchmarking` 的完整协议；随后给出九行性能表：

| Step | 技术 | 时间 | 加速比 |
|------|------|------|--------|
| 1 | 同步 load + MMA | 70 ms | 1× |
| 2 | K 循环累加 | — | — |
| 3 | 空间分块 | 53.6 ms | ~1.3× |
| 4 | TMA 异步加载 | 0.49 ms | ~142× |
| 5 | 软件流水线 | — | — |
| 6 | 持久内核 | — | — |
| 7 | warp 特化 | 0.23 ms | ~309× |
| 8 | 双 CTA cluster | 0.104 ms | ~676× |
| 9 | 多消费者 | 0.094 ms | ~744× |
| — | cuBLAS（参考） | 0.094 ms | ~744× |

再看表格脚注对可比性边界的说明：

> [chapter_gemm_advanced/index.md:879-883](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L879-L883)

这段交代三件事：① 每个有实测时间的行都用同一个 `M=N=K=4096` 问题，可以直接比较；② Step 1 的 70 ms 来自具有相同串行数据路径的全矩阵基线，**不是**入门章节里单 tile 内核 `hgemm_v1` 的一次运行——入门章用更小的问题讲解 Step 1–3，本表的 Step 1 与 Step 3 行测的是对应的全矩阵实现；③ Step 2 仍只计算一个输出 tile 故不可比，Step 5、6 是 TMA 内核与 warp 特化内核之间的中间版本、其机制都保留在 Step 7 中，表格只给区间端点；④ 这些数字来自一次 B200 参考运行，目的是在本教程的版本之间做同条件比较，而非代表其他问题规模或环境下的峰值性能。

最后看绘图脚本中的精确数据：

> [img/scripts/gen_gemm_perf.py:6-8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py#L6-L8)

`steps` 列出六个数据点（Step 3、4、7、8、9 与 cuBLAS），`times` 给出精确到微秒量级的耗时：`[53.642159, 0.493814, 0.226613, 0.103529, 0.094139, 0.094139]`。注意脚本数据从 Step 3 开始，不含 Step 1 的 70 ms。

> [img/scripts/gen_gemm_perf.py:14-28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py#L14-L28)

绘图部分：纵轴设为对数（`set_yscale('log')`）、纵轴范围 (0.06, 120) ms、每根柱子上方按耗时量级选择小数位数标注。`plt.savefig('../gemm_perf.png')` 使用相对路径，所以必须从 `img/scripts` 目录运行脚本（见 [img/scripts/README.md:3-9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L3-L9)，README 同时说明依赖为 `matplotlib` 与 `numpy`）。

#### 4.1.4 代码实践

**实践目标**：用书中数据独立复算表中所有累计加速比，验证表格数字与精确数据的自洽性。此实践不需要 GPU，只需要 Python。

**操作步骤**（示例代码，基于书中数据编写）：

```python
# verify_speedup.py —— 复算九步性能表的累计加速比（示例代码）
BASE_MS = 70.0  # Step 1 全矩阵基线，书中表格数值
rows = {
    "Step 3": 53.642159,
    "Step 4": 0.493814,
    "Step 7": 0.226613,
    "Step 8": 0.103529,
    "Step 9": 0.094139,
    "cuBLAS": 0.094139,
}
for name, t in rows.items():
    print(f"{name:>8}: {t:10.6f} ms  累计加速 {BASE_MS / t:8.1f}x")
```

**需要观察的现象**：输出应依次约为 1.3×、142×、309×、676×、744×、744×。

**预期结果**（据表中数据计算，待本地验证）：

| 行 | 精确耗时 ms | 复算累计加速比 | 表中标注 |
|----|------------|--------------|---------|
| Step 3 | 53.642159 | 70/53.642 ≈ 1.30× | ~1.3× |
| Step 4 | 0.493814 | ≈ 141.7× | ~142× |
| Step 7 | 0.226613 | ≈ 308.9× | ~309× |
| Step 8 | 0.103529 | ≈ 676.1× | ~676× |
| Step 9 | 0.094139 | ≈ 743.5× | ~744× |

复算值与表中 "~" 标注全部吻合，说明表中的近似加速比就是以 Step 1 的 70 ms 为分母、用精确耗时算出的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Step 2、5、6 三行的 Time 和 Speedup 是破折号？

**答案**：Step 2 的内核仍只计算一个输出 tile，与全矩阵结果没有可比性；Step 5、6 是 TMA 加载内核（Step 4）与 warp 特化内核（Step 7）之间的中间版本，其机制（软件流水线、持久调度）都完整保留在 Step 7 中，表格只给出该区间的端点。见 [chapter_gemm_advanced/index.md:881](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L881)。

**练习 2**：表中 70 ms 是不是入门章节单 tile 内核 `hgemm_v1` 的实测时间？

**答案**：不是。书中明确说明 70 ms 来自具有相同串行数据路径的全矩阵基线；入门章用更小的问题讲解 Step 1–3，本表 Step 1 与 Step 3 行测的是对应的全矩阵实现（[chapter_gemm_advanced/index.md:879](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L879)）。把 70 ms 当作 `hgemm_v1` 的时间是读表时的典型错误。

**练习 3**：`gen_gemm_perf.py` 的图里为什么没有 Step 1 的柱子？如果想加上它，要改脚本的哪两处？

**答案**：脚本 `steps`/`times` 数据列表只收录了六个数据点、未含 Step 1（[img/scripts/gen_gemm_perf.py:6-7](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py#L6-L7)）。想加上它需要在两个列表头部各插入 `'Step 1\nBaseline'` 与 `70.0`，并在 `colors` 补一个颜色；由于 `70 < 120`，现有 `set_ylim(0.06, 120)` 仍能容纳（[img/scripts/gen_gemm_perf.py:8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py#L8)、[L17](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py#L17)）。

### 4.2 模块二：加速归因

#### 4.2.1 概念说明

有了数字，下一步是回答"每一段增益是哪个机制带来的"。归因的第一纪律是：**一个比较区间的增益属于该区间新增的全部机制，不能记到单一机制头上**。书为此只定义了四个"可归因"的比较区间，每个区间都有明确的新增机制集合；区间内部的机制混在一起，区间之间才干净。

书还把九个版本的优化提炼成两条反复出现的目标（[chapter_gemm_advanced/index.md:896](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L896)）：

1. **别让 Tensor Core 等数据**——Step 4–7 围绕数据供给：TMA 搬 tile、双缓冲预备下一个 K 块、持久调度让 CTA 不空转、warp 特化让加载/MMA/回写并发推进。
2. **每片搬上芯片的数据多算几次**——Step 8–9 围绕片上复用：双 CTA cluster 产出更大输出 tile 让每个 A/B tile 参与更多乘加，第二个消费者让两个 A 块共享同一 B tile。

这两条正好对应 u3-l3 优化阶梯的"重叠"与"复用"两级，也正好按 4–6 步 / 7–8 步 把表格切成两半。

#### 4.2.2 核心流程

归因的操作流程是"区间差分 + 机制核对 + 定量验证"：

1. **列出区间**：Step 1→4、4→7、7→8、8→9（书定义的四个比较，见 4.2.3 源码引用）。
2. **对每个区间列新增机制**：从区间的起点版本到终点版本，scope/layout/dispatch 三要素各改了什么。
3. **把机制映射到资源通道**：数据搬运（带宽/AI）、执行重叠（空闲消除）、片上复用（AI 抬升）。
4. **定量核对**：对复用类增益，用算术强度公式验算；对总增益，换算 TFLOPS 看它占峰值的比例。

定量工具是两个公式。区间加速比：

\[ S_{\text{interval}} = \frac{t_{\text{before}}}{t_{\text{after}}} \]

以及 GEMM 吞吐（附录给出的形式，\(t_{\mu s}\) 为微秒延迟）：

\[ \text{TFLOP/s} = \frac{2 \times M \times N \times K}{t_{\mu s} \times 10^{6}} \]

`M=N=K=4096` 时 \(2MNK = 2 \times 4096^3 \approx 137.44\) GFLOP，于是 Step 9 的 0.094139 ms 换算约 \(137.44 \times 10^{9} / 94.139 \approx 1459\) TFLOP/s——相对书中性能章节取整的 B200 稠密 fp16 峰值约 2 PFLOP/s，达到约 73%。cuBLAS 同为 0.094 ms，这就是"对齐 cuBLAS"的定量含义。

对 Step 7/8/9 的复用增益还可以用 tile 级 AI 验算（依据书中机制的推算）：以"每 CTA（或每 cluster）每个 K 块"为记账单位，计算量除以 staged 字节数：

- **Step 7**（单 CTA，128×128 输出）：每 K 块 staged \((128+128)\times 64 \times 2\text{B} = 32\text{KB}\)，计算 \(2\times 128\times 128\times 64\) FLOP，\(AI \approx 64\) FLOP/byte；
- **Step 8**（cluster 256×256 输出，两 CTA 各出 1 片 A + 1 片 B，A 切片乘以两片 B）：staged 字节翻倍但计算量变为 4 倍，\(AI \approx 128\) FLOP/byte；
- **Step 9**（cluster 512×256 输出，4 片 A + 2 片 B）：计算量相对 Step 8 翻倍、staged 字节只增 1.5 倍，\(AI \approx 128 \times \tfrac{4}{3} \approx 170.7\) FLOP/byte。

数字走势（64 → 128 → 170.7）与实测加速走势（2.2× → 1.10×）方向一致、幅度递减，这正是归因想得到的"机制解释数字"的证据链。

#### 4.2.3 源码精读

四个比较区间的官方定义：

> [chapter_gemm_advanced/index.md:885-890](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L885-L890)

这段列出四个区间并给出归因要点：

1. **Step 1 → Step 4**（70 ms → 0.49 ms，约 142×）：该区间同时加入 K 循环、空间分块、多 CTA 并行与 TMA，因此全部增益不能只归给 TMA。
2. **Step 4 → Step 7**（0.49 → 0.23 ms，约 2.2×）：软件流水线、持久调度与 warp 特化的合计贡献。
3. **Step 7 → Step 8**（0.23 → 0.104 ms，约 2.2×）：双 CTA 协作 MMA 提高 staged 操作数的复用。
4. **Step 8 → Step 9**（0.104 → 0.094 ms，约 10%）：第二个 MMA 消费者复用同一批 staged B 切片。

归因叙述的总结：

> [chapter_gemm_advanced/index.md:896-902](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L896-L902)

这段把九步串成因果故事：Step 1–3 从单 tile 出发加 K 循环、按 M/N 分块覆盖全矩阵；Step 4–7 改善数据供给（TMA 搬运、双缓冲预备、持久调度器维持 CTA 忙碌、warp 特化让三路并发），到 Step 7 时 Tensor Core 不必再等整条加载或回写路径结束；最后两步提高复用（更大输出 tile、两 A 块共享 B tile），让每次从 GMEM 的搬运支撑更多片上计算。结论句强调：结果是"数据搬运、执行重叠与片上复用三方面多种优化的协调"，而非任何单一机制的功劳。

每个区间的机制依据都在本章前部的内核源码里，归因时可回查：

- **Step 7 三角色与四道屏障**（区间 2 的机制）：角色表见 [chapter_gemm_advanced/index.md:52-56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L52-L56)，屏障表见 [L62-67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L62-L67)。
- **Step 8 两 CTA 切分与 DSMEM 互读**（区间 3 的机制）：A/B/D 划分表见 [chapter_gemm_advanced/index.md:351-354](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L351-L354)；TMA 到达字节数按 `CTA_GROUP * (BLK_M*BLK_K + BLK_N*BLK_K) * F16_SIZE` 合账登记见 [L390-394](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L390-L394)。
- **Step 9 消费者划分与共享 B**（区间 4 的机制）：两个消费者各算 256 行、共 256 列、各占 TMEM 一段列区间的表见 [chapter_gemm_advanced/index.md:612-615](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L612-L615)；"每 stage 装 2 块 A + 1 块 B"的字节数公式 `CTA_GROUP * (NUM_CONSUMER * BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE` 见 [L645-651](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L645-L651)——这正是 4.2.2 中 AI 推算里"4 片 A + 2 片 B"的源码出处。

#### 4.2.4 代码实践

**实践目标**：改编仓库自带的 `gen_gemm_perf.py`，在重绘曲线的同时输出四个区间的加速比与各行 TFLOPS，把"读图"变成"算账"。此实践只需 `matplotlib`/`numpy`，无需 GPU。

**操作步骤**：

1. 进入 `img/scripts` 目录，**不要直接运行原脚本**——它的 `savefig('../gemm_perf.png')` 会覆盖仓库已检入的图片，而本套讲义不允许修改源码。
2. 把脚本复制到讲义目录再改，例如 `cp img/scripts/gen_gemm_perf.py modern-gpu-programming-for-mlsys-tutorial/my_gemm_perf.py`。
3. 在副本中做三处修改（示例代码）：

```python
# my_gemm_perf.py 片段 —— 在 gen_gemm_perf.py 基础上修改（示例代码）
M = N = K = 4096
flop = 2 * M * N * K                      # ≈ 137.44 GFLOP

# 1) 在柱状标签旁追加 TFLOPS
for x, t in enumerate(times):
    tflops = flop / (t * 1e-3) / 1e12     # t 单位 ms
    ax.text(x, t * 1.20, f"{tflops:.0f} TFLOP/s", ha="center",
            va="bottom", fontsize=7, color="#666")

# 2) 输出四个比较区间的加速比
base = 70.0                                # Step 1 全矩阵基线（书中表格）
pairs = [("Step1->Step4", base, times[1]), ("Step4->Step7", times[1], times[2]),
         ("Step7->Step8", times[2], times[3]), ("Step8->Step9", times[3], times[4])]
for name, a, b in pairs:
    print(f"{name}: {a/b:.2f}x")

# 3) 输出路径改到讲义目录，避免覆盖仓库图片
plt.savefig("my_gemm_perf.png", dpi=150, bbox_inches="tight")
```

4. 运行 `python my_gemm_perf.py`。

**需要观察的现象**：终端打印四个区间加速比；生成的 `my_gemm_perf.png` 与书中 `img/gemm_perf.png` 同构（对数纵轴、六根柱子），柱顶多出 TFLOPS 标注。

**预期结果**（据书中数据计算，待本地验证）：

| 区间 | 计算 | 加速比 |
|------|------|--------|
| Step 1→4 | 70 / 0.493814 | ≈ 141.7× |
| Step 4→7 | 0.493814 / 0.226613 | ≈ 2.18× |
| Step 7→8 | 0.226613 / 0.103529 | ≈ 2.19× |
| Step 8→9 | 0.103529 / 0.094139 | ≈ 1.10× |

TFLOPS 标注依次约为：Step 3 ≈ 2.6、Step 4 ≈ 278、Step 7 ≈ 607、Step 8 ≈ 1327、Step 9 ≈ 1459、cuBLAS ≈ 1459。注意 Step 9 与 cuBLAS 完全相同——因为二者实测时间相同（0.094139 ms）。

#### 4.2.5 小练习与答案

**练习 1**：为什么书说"整个 142× 不能全部归因于 TMA"？如果想更干净地度量 TMA 本身的贡献，表中哪两个行的对比更合适？

**答案**：因为 Step 1→4 区间同时加入了 K 循环、空间分块、多 CTA 并行与 TMA 多个机制（[chapter_gemm_advanced/index.md:887](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L887)）。表中 Step 3 与 Step 4 两行都已具备全矩阵分块与多 CTA 并行，差异集中在加载路径（线程协作拷贝改 TMA 派发及配套同步），53.642159 / 0.493814 ≈ 108.6× 是更接近"TMA 净效应"的对比。（此 Step 3→4 对比为本讲义补充的观察，书只定义四个官方区间。）

**练习 2**：Step 8 相对 Step 7 又得到约 2.2×，机制上从哪里来？

**答案**：双 CTA cluster 用一次 `cta_group=2` 协作 MMA 产出 256×256 输出 tile：A 沿 M 对半、各 CTA 就近加载，两片 B 被双方输出共用，硬件经 DSMEM 读对端 Bsmem。staged 操作数参与的乘加翻倍，tile 级算术强度从约 64 升到约 128 FLOP/byte（每 K 块 staged 字节 ×2、计算量 ×4），GMEM 流量压力减半（u13-l2 已建立该结论，本讲 4.2.2 给出验算）。

**练习 3**：Step 9 只比 Step 8 快约 10%，远小于 Step 8 的 2.2×。既然 AI 提升比例相近（×2 与 ×4/3 只差一档），为什么实测收益差这么多？

**答案**：三方面叠加：① AI 增幅本身递减（128→170.7 是 ×1.33，小于 64→128 的 ×2）；② 到 Step 8 时内核已接近 cuBLAS 水平（0.104 vs 0.094 ms），剩余优化空间只有约 10%，说明瓶颈已从 GMEM 供给转移到别处（Tensor Core 发射、epilogue、调度等）；③ 共享 B 只减少 B 的重复搬运，A 的加载量与回写量都随输出规模线性增长，这部分无法被该机制削减。

### 4.3 模块三：基准条件

#### 4.3.1 概念说明

性能表脚注的最后一句话划定了数字的适用范围："这些数字来自一次 B200 参考运行，用于在本教程的版本之间做同条件比较，而非代表其他问题规模或环境下的峰值性能。"这句话把两种主张分开：

- **版本间比较**（本书的用法）：所有版本同机、同规模、同协议测量，除内核外其余变量全部固定，除法才有意义。
- **峰值性能主张**（不是本书的用法）：断言"这个内核能达到 X TFLOPS"，需要额外论证问题规模、时钟、缓存状态等条件，且通常要在多个规模下扫点。

要让自己的复测可与书中数字对话，或者让自己的两个内核版本可比，就必须控制一组变量。附录 `chap_benchmarking` 把这套纪律总结为：测量与诊断分开、计时前先验证正确性、明确计时边界、选对计时器、保持条件一致、记录环境版本。本模块把这些要求落成一份可勾选的清单。

其中两个条件值得单独解释：

- **锁定时钟（locked clocks）**：GPU 默认按温度与功耗动态调频。若不锁定，先测的版本可能在冷机高频下运行、后测的在热机降频下运行，测量顺序本身引入系统偏差。
- **1000 次计时迭代**：单次计时含启动抖动，多次迭代取统计量才能稳定。迭代次数与预热（warm-up）预算配套——预热不足时前几轮结果仍在下降，附录要求"增加 warm-up 直到早期轮次停止漂移"。

#### 4.3.2 核心流程

复现这套端到端实验的协议流程：

1. **固定问题与数值语义**：`M=N=K=4096`，fp16 输入/输出、fp32 累加，转置约定、对齐与容差全版本一致。
2. **正确性先行**：每个版本先与同一参考实现（如 fp32 计算后再转 fp16 的 PyTorch GEMM）在声明容差下断言通过，才允许进入计时；参考计算与比对放在计时区间之外。
3. **定义计时边界**：明确被测操作只含内核本身（不含输入构造、分配、编译），所有版本用同一边界。
4. **选择计时器**：书用 `tvm.tirx.bench`（CUDA events 计时；每次测量前写 256 MiB 缓冲以减少 L2 残留复用，该写在计时区间之外）。
5. **统一预算**：预热与重复预算（如每版本 1000 次计时迭代）、轮数、冷却时间对所有版本相同。
6. **控制环境**：锁定时钟；多版本对比时交替测量顺序，避免某版本总是"更冷"或"更热"。
7. **记录与报告**：记录 GPU、驱动、CUDA、框架、编译器版本与 dtype、形状、时钟、功耗设置；报告每轮结果并说明汇总用的是均值还是中位数。

#### 4.3.3 源码精读

表中给出的测量条件：

> [chapter_gemm_advanced/index.md:862-864](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L864)

"测量使用 NVIDIA B200、`M=N=K=4096`、fp16 输入、锁定时钟、每个被测版本 1000 次计时迭代；新的测量与复现尝试应遵循 `chap_benchmarking` 的完整协议。"——这一句同时给出五个测量条件与协议出处。

计时前先验证正确性：

> [appendix/benchmarking_gpu_kernels.md:28-36](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L28-L36)

附录要求四步：构造有代表性的输入（含边界情形）、运行实现并同步使 GPU 工作完成、与参考实现按声明容差比对、若内核原地修改状态则在每次检查前恢复同一初始状态。对自定义 GEMM，参考可取 fp32 计算后转目标 dtype 的 PyTorch GEMM，且参考计算与结果比对保持在计时区间之外。

书中使用的计时工具：

> [appendix/benchmarking_gpu_kernels.md:166-198](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L166-L198)

本书用 TVM 的 `tvm.tirx.bench` 处理预热、重复计时与统计：被测函数只负责发起已准备好的实现，输入、输出与工作区都留在计时区间之外；与手动暖缓存示例不同，`bench` 在每次被测调用前写一个 256 MiB 缓冲以减少上一次调用残留在 L2 中的数据被复用，然后记录独立的 CUDA event 区间。`warmup`/`repeat` 是毫秒预算（经短校准换算成调用次数），`rounds=5` 跑五轮、`cooldown_s=1.0` 在每轮前暂停；`impls` 存五轮均值，`round_samples` 存逐轮结果。对比实现时须用同一组设置。

条件一致性与交替测量：

> [appendix/benchmarking_gpu_kernels.md:338-369](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L338-L369)

这一节要求：保留每轮结果并说明汇总是中位数还是均值；结果随轮次持续漂移时先检查预热、温度与时钟状态；对比实现时交替测量顺序，避免某实现总在更冷或更热的设备上测；全程用同一种缓存策略（手动例子重复用同一组矩阵代表暖缓存负载，TVM 的 event/Proton 计时器则每次测量前写 256 MiB 缓冲减少 L2 复用——该写在计时区间外）；同时对齐数值语义（dtype、布局、转置约定、对齐、累加精度、容差等）、被测范围（是否含分配、转换、状态复位等）与调优条件（workspace 上限、是否允许按形状自动调优及搜索预算）；最后记录 GPU、驱动、CUDA、框架、编译器版本与 dtype、形状、时钟、功耗设置，库基线还要记录库版本、所选算法与 workspace。

延迟到吞吐的换算：

> [appendix/benchmarking_gpu_kernels.md:371-379](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L371-L379)

附录给出公式 \(\text{TFLOP/s} = 2 \times M \times N \times K / t_{\mu s} / 10^{6}\)，并强调分子计的工作量与计时边界必须描述同一件事：若计时区间覆盖"GEMM+ReLU"完整操作，除出来的只是有效吞吐，要报 GEMM 内核自身的 TFLOP/s 就必须让计时区间只含 GEMM。

#### 4.3.4 代码实践

**实践目标**：为"在 B200 上复现九步性能表"写出一份变量控制清单。这是纯文档型实践，无需 GPU；写清单的过程就是检验你是否理解协议的过程。

**操作步骤**：

1. 通读 [appendix/benchmarking_gpu_kernels.md:338-369](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L338-L369) 的"Keep Benchmark Conditions Consistent"一节。
2. 对照下表逐项写出你的复现取值（下表"依据"列标注每项在书中的出处）：

| 变量类别 | 要固定的内容 | 依据 |
|---------|-------------|------|
| 硬件与环境 | GPU 型号（B200）、驱动、CUDA、TVM/PyTorch 版本，全部记录 | 表格脚注 L862-864；附录 L366-369 |
| 问题与数值语义 | M=N=K=4096；fp16 输入输出、fp32 累加；D=ABᵀ 约定；容差 | L862-864；附录 L357-359 |
| 正确性门槛 | 每版本先对同一 fp32 参考断言通过再计时；比对在计时区间外 | 附录 L28-36 |
| 计时边界 | 只含内核调用；输入构造、分配、编译、参考比对全部在外 | 附录 L63-69 |
| 计时器与预算 | `tvm.tirx.bench`（timer="event"）；预热/重复预算、轮数、冷却时间全版本一致；1000 次计时迭代 | L862-864；附录 L166-198 |
| 时钟与热状态 | 锁定时钟；结果随轮次漂移时检查温度/功耗/预热 | L862-864；附录 L344-347 |
| 缓存策略 | 统一采用 bench 的 256 MiB 预写（或声明暖缓存），不混用 | 附录 L349-356 |
| 测量顺序 | 九个版本交替测量，不让某版本总在更冷/更热设备上 | 附录 L346-347 |
| 报告口径 | 保留逐轮结果，声明均值/中位数；TFLOPS 附延迟与工作量口径 | 附录 L344-345、L381-390 |
| 库基线 | cuBLAS 记录库版本、所选算法、workspace | 附录 L366-369 |

3. 在清单末尾写一段"可比性声明"：本次复现结果仅用于同条件下版本间比较，不构成对其他问题规模或环境的峰值性能主张（呼应 [chapter_gemm_advanced/index.md:883](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L883)）。

**需要观察的现象**：无运行现象；成果是清单文档本身。可自查的检验标准：清单中每一项都能指向附录或正文的一个具体条目，且没有一项是"凭感觉"加的。

**预期结果**：清单覆盖上表十类变量；若你的清单出现附录没有依据的条目（例如"必须关闭其他进程"），要么找到书中依据，要么标注为"本讲义补充建议"。

#### 4.3.5 小练习与答案

**练习 1**：为什么协议要求"锁定时钟"，而且对比多个实现时要交替测量顺序？

**答案**：GPU 会按温度与功耗动态调频，不锁定时钟时，测量顺序会系统性偏向先测的版本（冷机高频）或后测的版本；附录还要求结果随轮次持续漂移时检查预热、温度与时钟状态（[appendix/benchmarking_gpu_kernels.md:344-347](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L344-L347)）。交替顺序保证没有任何实现总在更冷或更热的设备状态下被测量。

**练习 2**：手动 CUDA events 示例与 `tvm.tirx.bench` 的缓存策略有何不同？为什么这属于"必须统一"的条件？

**答案**：手动示例反复复用同一组矩阵，代表暖缓存工作负载（实际命中率还取决于数据总量与缓存容量）；`bench` 在每次被测调用前写 256 MiB 缓冲以减少上一次调用残留在 L2 的复用，该写在计时区间之外（[appendix/benchmarking_gpu_kernels.md:349-356](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L349-L356)）。GEMM 这类大规模访存内核对 L2 残留很敏感，若两个版本用不同缓存策略测，差异可能来自缓存而非内核。

**练习 3**：报告"Step 9 达到约 1459 TFLOP/s"时，按附录要求还应同时交代什么？

**答案**：附上底层延迟测量（0.094139 ms）并说明工作量如何计数：分子 \(2 \times 4096^3\) 是完整稠密 GEMM 的工作量，计时区间必须只覆盖该内核；若计时边界覆盖了更多操作（如 epilogue 之外的准备），得到的只是"有效吞吐"，须另行命名（[appendix/benchmarking_gpu_kernels.md:381-390](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L381-L390)）。

## 5. 综合实践

**任务**：制作一份"九步 GEMM 优化性能分析报告"，把本讲三个模块串起来。全程无需 GPU。

**第一步：重绘曲线并扩展标注。** 把 `img/scripts/gen_gemm_perf.py` 复制到讲义目录，改编为：① 加入 Step 1 基线（70 ms）的柱子；② 柱顶同时标注耗时与 TFLOPS；③ 输出路径指向讲义目录。运行得到 `my_gemm_perf.png`。

**第二步：算账。** 在同一脚本中打印：六个实测点的累计加速比、四个官方区间的区间加速比、每个点的 TFLOPS 及其占约 2 PFLOP/s 峰值的百分比（Step 9 ≈ 73%，待本地验证）。

**第三步：写归因分析。** 对每个区间写 3–5 句话：该区间新增了哪些机制（引用本章内核源码的具体位置，如 4.2.3 列出的角色表、切分表、字节公式）、机制作用于哪条资源通道（数据搬运 / 执行重叠 / 片上复用）、有没有定量验证（如 Step 8 的 AI 64→128 FLOP/byte 验算）。特别要求：区间 1 必须说明"不能全归 TMA"，并补充你自己的 Step 3→4 观察（≈108.6×）。

**第四步：附变量清单与可比性声明。** 把 4.3.4 的十类变量清单与"同条件版本间比较、非峰值主张"的声明附在报告末尾，说明若要在 B200 上复现，须用 `tvm.tirx.bench` 按附录协议执行。

**预期成果**：一张扩展版性能图、一张四区间加速比与 TFLOPS 表、一段逐区间归因文字、一份复现变量清单。完成后你就把九个版本从"一列数字"还原成了"一组机制因果链"，这正是本讲想训练的能力。

## 6. 本讲小结

- 九步性能表来自一次 B200 参考运行：`M=N=K=4096`、fp16、锁定时钟、每版本 1000 次计时迭代；70 ms → 0.094 ms（约 744×），Step 9 与 cuBLAS 实测时间相同。
- 表中 Step 2/5/6 用破折号是可比性设计：Step 2 只算单 tile，Step 5/6 的机制都保留在 Step 7 中；70 ms 是全矩阵串行基线，不是单 tile `hgemm_v1`。
- 书只定义四个可归因区间：1→4 约 142×（K 循环+分块+多 CTA+TMA 的合计，不能全归 TMA）、4→7 约 2.2×（流水线+持久调度+warp 特化）、7→8 约 2.2×（cluster 复用，AI 64→128 FLOP/B）、8→9 约 10%（共享 B，AI ×4/3）。
- 两条反复出现的优化主线：别让 Tensor Core 等数据（Step 4–7 的供给与重叠），每片搬上芯片的数据多算几次（Step 8–9 的复用）。
- 复现或比较必须控制的条件：正确性先行、统一计时边界与计时器、锁定时钟、统一缓存策略与预算、交替测量顺序、记录环境版本；结论只构成同条件版本间比较，不构成峰值性能主张。
- 书中数字可用纯 Python 复算与重绘：`gen_gemm_perf.py` 携带六个精确数据点，改编时须把输出路径改到仓库之外，避免覆盖检入图片。

## 7. 下一步学习建议

- **进入 Part IV（Flash Attention 4）**：FA4 把本讲的全部技术（TMA、tcgen05、TMEM、多角色流水线、cluster 复用）综合到一个带在线 softmax 的真实内核中，从 u14-l1 的算法结构开始。
- **补齐基准与剖析工具链**：本讲只用到附录的测量协议；u15-l4 至 u15-l6 将覆盖 CUDA events 计时脚本、Proton/Nsight Systems/Nsight Compute 三级剖析与 IKET 内核内标注，让你不仅会测总数，还能定位时间花在哪。
- **源码延伸阅读**：把 `appendix/benchmarking_gpu_kernels.md` 的 "Keep Benchmark Conditions Consistent" 与 "Convert Latency to Throughput" 两节通读一遍；再回看 `chapter_gemm_basics` 与 `chapter_gemm_async`，对照本讲表格体会 Step 1–6 各自解决的是表中哪一段。
- 若你手头有 Blackwell GPU：按第 5 节综合实践的清单规划一次真实复测，用 `tvm.tirx.bench`（timer="event"）逐版本计时，与书中数字对照并分析差异来源。
