# 基准评测与对标 CPU/参考

## 1. 本讲目标

经过前面十几讲，你已经能让一个加速内核「跑起来」并验证它「算得对」。但「算得对」并不等于「值得用」——一个 FPGA/AIE 内核要真正有价值，还必须回答两个问题：**它比 CPU 快多少？它在可接受的误差范围内吗？**

本讲把视角从「单个内核能不能用」抬升到「如何用工程化的方式量化和报告一个内核的质量」。学完后你应该能够：

1. 读懂各库 `benchmarks/` 目录的统一组织方式，并能区分它与 `tests/` 目录在目标上的根本差异。
2. 识别加速库里「与参考模型比对」的三种典型范式：bit 精确、绝对误差阈值、相对/范数误差阈值。
3. 解释为什么数值类内核（FFT、SVD）几乎永远做不到 bit 精确，以及如何用一个合理的误差阈值来判定 PASS/FAIL。
4. 解读 README 与日志里频率、延迟、吞吐、资源利用率四类性能指标，并区分主机端测时与设备端 profiling 两种计时手段。

本讲依赖 u4-l2（主机控制链与 `xrt::bo`）与 u7-l2（solver 的 AIE/L2 基准）。涉及的代码全部来自真实仓库，关键点均附永久链接。

## 2. 前置知识

阅读本讲前，建议你先具备以下概念（前序讲义已建立）：

- **tests vs benchmarks 的分工**：`tests/` 验证「对错」（功能正确性），`benchmarks/` 量化「快慢与面积」（性能与资源）。但二者并非完全独立——一个会算错数的基准是毫无意义的，所以 benchmark 内部通常**也**包含正确性校验。
- **XRT 主机控制链**（u4-l2）：`xrt::device → load_xclbin → xrt::kernel → xrt::run → set_arg/start/wait`，以及 `xrt::bo` 的 `map/sync`。本讲的 DSP 基准就运行在这条链上。
- **L1 大写 TARGET 与 L2 小写 target**（u2-l3、u5-l1）：`csim/csynth` 给出 II/latency/资源的**综合估计**，而 `hw/hw_emu` 给出**真实运行时延**。本讲的「性能指标」横跨这两套来源。
- **定点与浮点的数值差异**：HLS 内核常用 `ap_fixed` 或定标整数，而 CPU 参考模型常是 `float/double`，两者的舍入与截断顺序不同，结果不会逐位相等。

一个贯穿全讲的直觉：**数值计算没有「绝对正确」，只有「相对误差是否可接受」**。基准评测的核心工程问题，就是把这句模糊的话变成一行可判定的 `if`。

## 3. 本讲源码地图

本讲横跨多个库，但每个文件只看其中与「评测」相关的一小段。建议按下表先建立方位感：

| 文件 | 角色 | 本讲关注的部分 |
|------|------|----------------|
| [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) | PL+AIE 端到端示例的主机程序 | 末尾的结果校验段（`ref_output.txt` 比对与 `level` 阈值） |
| [dsp/L2/examples/vss_fft_ifft_1d/data/ref_output.txt](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/data/ref_output.txt) | 预先计算的黄金参考输出 | 文本格式（一行一个实/虚部整数） |
| [solver/L2/benchmarks/gesvj/test_gesvj.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp) | SVD 基准的 host 程序 | CPU 端重建 `A_out=U·Σ·Vᵀ` 并与原矩阵比 L2 范数 |
| [solver/L2/benchmarks/gesvj/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md) | SVD 基准的说明与性能表 | 「用 Lapack 校验」与频率/资源/延迟表 |
| [security/L1/benchmarks/crc32/host/main.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp) | CRC32 基准的 host 程序 | bit 精确比对 + OpenCL profiling 计时 |
| [security/L1/benchmarks/crc32/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/README.md) | CRC32 基准说明 | 示例输出与 4.7 GB/s 吞吐表 |
| [security/L1/benchmarks/crc32/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/description.json) | 用例元数据 | `category: canary` 与 hw 三档目标 |
| [blas/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md) | BLAS 库说明 | 指向外部 benchmark 结果页 |

总览：`dsp` 给出「绝对误差阈值」样本，`solver` 给出「范数误差阈值」样本，`security` 给出「bit 精确」样本与设备端计时样本，三者合起来正好覆盖本讲的全部最小模块。

## 4. 核心概念与源码讲解

### 4.1 benchmarks 目录的组织与用途

#### 4.1.1 概念说明

在前序讲义里你已经多次见到 `tests/` 目录——它回答「这个内核算得对不对」。本讲的主角是它的姊妹目录 `benchmarks/`，回答「这个内核算得多快、占多少面积」。

两者的关键区别：

| 维度 | `tests/`（测试） | `benchmarks/`（基准） |
|------|------------------|----------------------|
| 首要目标 | 功能正确性 | 性能与资源 |
| 输入规模 | 小（够触发边界即可） | 大（够压满带宽/算力） |
| 典型数据 | 几 KB ~ 几十 KB | 几十 MB ~ 几百 MB |
| 必须有参考校验？ | 必须 | 通常也有（但重点在计时） |
| 报告产物 | PASS/FAIL | 频率 / 延迟 / 吞吐 / 资源表 |

加速库的 `benchmarks/` 目录散落在多个库里，按库与层次组织：

- `solver/L2/benchmarks/`：`gesvj`、`gesvdj`、`gtsv`（PL HLS 路线，跑 Alveo）。
- `security/L1/benchmarks/`：`crc32`、`adler32`（PL HLS 路线，跑 U50）。
- `vision/L3/benchmarks/`：`blobfromimage`、`colordetect`（多内核流水线，与其它架构对比）。
- `dsp/L2/`：同时含 `examples/`（含本讲的 `vss_fft_ifft_1d`）与 `benchmarks/`。
- `blas`：README 直接指向 [外部 benchmark 结果页](https://docs.xilinx.com/r/en-US/Vitis_Libraries/blas/benchmark.html)。

`vision/L3/README.md` 用一句话点明了 benchmarks 的定位：它是一组「可构建的应用，会输出与其它架构的性能对比」。

#### 4.1.2 核心流程

每个 benchmark 目录的组织与 `tests/` 高度同构（都是「目录即用例」），但多了两样东西：

1. **更大的输入数据**：通常带一个 `test.dat` / `*.txt` 数据包，体积远大于功能测试。
2. **README 里的 Profiling 表**：列出该内核在某块板子上的频率、资源、吞吐。

一个 benchmark 的运行链路：

```
cd <lib>/L?/benchmarks/<name>
  → source Vitis/XRT 环境
  → export PLATFORM=... ; export TARGET=hw
  → make run                # 编译 xclbin + host（耗时可达数小时）
  → ./host.exe -xclbin ... -data ... [-num/-M/-N/-runs ...]   # 上板跑
  → 终端打印 Execution time / Kernel time / ...
  → 查 README 的 Profiling 表对照
```

#### 4.1.3 源码精读

以 `security/L1/benchmarks/crc32` 为样本，看一个基准目录里有什么。它的 [description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/description.json) 把它登记为 CI 可识别的用例，关键字段是 `flow`、平台白名单与 CI 调度信息：

```json
"flow": "system",
"platform_allowlist": [ "vck190", "aws-vu9p-f1" ],
...
"testinfo": {
    "targets": [ "vitis_hw_emu", "vitis_hw_build", "vitis_hw_run" ],
    "category": "canary"
}
```

- [description.json:5-6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/description.json#L5-L6)：`flow=system` 走 L2/L3 的 v++ 三段流程；平台白名单限定它能跑的板子。
- [description.json:99-105](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/description.json#L99-L105)：`targets` 是 v++ 三档（hw_emu 仿真 / hw_build 构建 / hw_run 上板），`category: canary` 表示这是「全量必跑」档（与 `fast`/`full` 相对，详见 u14-l1）——也就是说，benchmark 同样要被 CI 调度，并不是写完就放着。

[README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/README.md) 则点出基准的「大输入」特征：

- [README.md:4](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/README.md#L4)：「为评测 crc32 性能，我们准备了一个 **268,435,456 字节**（256 MB）的消息数据包作为内核输入」——这正是基准与功能测试最直观的差别：数据量被刻意撑大到能压满带宽。

#### 4.1.4 代码实践

**实践目标**：用目录列举的方式，建立对全仓库 `benchmarks/` 分布的整体印象。

**操作步骤**：

1. 在仓库根目录执行（只读命令）：
   ```bash
   ls solver/L2/benchmarks security/L1/benchmarks vision/L3/benchmarks
   ```
2. 对每个目录打开它的 `README.md`，找到「Profiling」一节，记录它声明的目标板子（如 U50、U250）与给出的吞吐数字。

**需要观察的现象**：三个库的 benchmark 各自针对不同的板子与路线（security→U50、solver→U250、vision→L3 应用对比），但目录组织（`Makefile` + `README.md` + `description.json` + `host/` + `kernel/`）高度一致。

**预期结果**：你会确认「目录即用例 + 大数据 + Profiling 表」是 benchmark 的统一形态。

**待本地验证**：各 README 表格里的具体频率/资源数字是历史测量值，未必与你本地板子完全一致。

#### 4.1.5 小练习与答案

**练习 1**：`security/L1/benchmarks/crc32/description.json` 里 `category` 是什么？它和 `tests/` 里某些用例的 `category` 含义是否相同？

**参考答案**：是 `canary`，含义与 u14-l1 讲的完全相同——CI 调度时全量必跑档。这说明 benchmark 与 test 共用同一套 `description.json` 元数据契约，只是它额外承担性能评测职责。

**练习 2**：为什么 CRC32 的基准输入要选 256 MB 这么大，而不是几 KB？

**参考答案**：吞吐类指标需要在「稳态」下测量才准。输入太小会让主机端启动/同步开销占比过高，掩盖内核真实带宽；256 MB 足以让数据搬运与计算进入稳态流水，此时测得的「字节/秒」才有意义。

---

### 4.2 参考模型比对的三种范式

#### 4.2.1 概念说明

「加速内核算得对不对」这句话，工程上必须落实为「把内核输出 `actual` 与某个参考值 `ref` 做比较」。问题是：**怎么比？** 加速库里存在三种截然不同的范式，理解它们的适用边界是本讲的核心：

1. **bit 精确（bit-exact）**：`actual == ref` 逐位相等。只有当运算是确定性的整数/位运算（如 CRC、哈希）时才成立。
2. **绝对误差阈值**：\(|actual - ref| \le \text{level}\)。用于定点/整数表示的数值内核，阈值是「定标后的整数单位」。
3. **相对/范数误差阈值**：把整批输出的相对误差或 L2 范数与一个浮点阈值比较，如 \(\text{errA} \le 10^{-4}\)。用于浮点数值内核（分解、求逆）。

为什么大多数数值内核做不到 bit 精确？三个根源（下一节会逐条对到代码）：

- **定点 vs 浮点**：HLS 内核常是 `ap_fixed` 或定标 `int`，CPU 参考是 `double`，量化误差天然存在。
- **浮点非结合律**：\((a+b)+c \ne a+(b+c)\)，加法顺序不同结果末位就不同。
- **迭代算法的合法多解**：SVD 的奇异值符号、特征向量方向都可能不同，但数学上等价——此时不能比「值」，只能比「重构关系」。

#### 4.2.2 核心流程

三种范式在主机端的判定骨架（伪代码）：

```
# 范式 1：bit 精确
if (actual != ref)  FAIL

# 范式 2：绝对误差阈值（DSP FFT）
err = abs(actual - ref)
if (err_re > level || err_im > level)  FAIL

# 范式 3：范数误差阈值（solver SVD）
err = sqrt( sum( (actual[i] - ref[i])^2 ) )
if (err > 0.0001)  FAIL
```

范式 2 与范式 3 的本质区别在「阈值是绝对量还是相对量」、以及「是逐元素判还是整体判」。绝对阈值简单但对量级敏感（大数容忍多、小数容忍少），范数阈值更贴近「整体近似程度」。

#### 4.2.3 源码精读

**范式 1：bit 精确（CRC32）**

CRC 是确定性的位运算，输出是单个 32 位校验值，因此可以直接逐位比。在 [security/L1/benchmarks/crc32/host/main.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp) 里，参考值是一个硬编码常量：

- [main.cpp:81](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L81)：`ap_uint<32> golden = 0xff7e73d8;` —— 黄金值直接写死。
- [main.cpp:221-227](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L221-L227)：`if (golden != crc_out) { ... nerr = 1; }` —— 严格的 `!=`，没有任何容差。这是 bit 精确范式的标志。

注意：能这么干，正是因为 CRC 没有数值误差，硬件实现与软件参考在数学上必然逐位相等。

**范式 2：绝对误差阈值（DSP FFT）**

转到数值内核，`dsp/L2/examples/vss_fft_ifft_1d/host.cpp` 的末尾给出了教科书式的绝对阈值判定。参考输出存在 `data/ref_output.txt` 里：

- [host.cpp:220](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L220)：`ss_o.open("ref_output.txt", ...);` 打开黄金文件。
- [host.cpp:233-251](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L233-L251)：核心校验循环，关键三行是：

```cpp
int level = (1 << 8);                                       // = 256
real_dtype err_re = abs(val_g_re - val_a_re);
real_dtype err_im = abs(val_g_im - val_a_im);
bool this_flag = (err_re > level) || (err_im > level);      // Reference model is not bit accurate
```

- `level = (1 << 8) = 256` 是**绝对误差阈值**，单位是 `cint32` 整数表示里的「最小整数单位」（FFT 内核用定标定点输出）。
- 注释 `// Reference model is not bit accurate` 一语道破：作者明确承认参考模型不 bit 精确，所以才需要这个阈值。
- `flag |= this_flag` 用按位或累加所有样本的失败标记——任何一个实部或虚部超阈值，整体就判失败。

**范式 3：范数误差阈值（solver SVD）**

`solver/L2/benchmarks/gesvj` 做的是 SVD（奇异值分解），SVD 有个特点：奇异值的符号、左右奇异向量的方向都有「合法多解」，所以不能直接比 `U`、`V` 的元素。这里的工程技巧是**重构原矩阵再比**：用分解出的 \(U,\Sigma,V\) 重算 \(A_{\text{out}} = U\Sigma V^T\)，与输入矩阵 \(A\) 比范数。在 [test_gesvj.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp) 里：

- [test_gesvj.cpp:264-265](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp#L264-L265)：`transposeMat<double>(...); MulMat(...);` 在 CPU 上重建 \(A_{\text{out}}\)。
- [test_gesvj.cpp:267-275](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp#L267-L275)：计算 L2 范数误差

```cpp
double errA = 0;
for (...) errA += (dataA_svd[...] - dataA_out[...]) * (dataA_svd[...] - dataA_out[...]);
errA = std::sqrt(errA);
```

即 \(\text{errA} = \sqrt{\sum_{i,j}(A_{ij} - A_{\text{out},ij})^2}\)。

- [test_gesvj.cpp:283-289](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp#L283-L289)：阈值判定 `if (errA > 0.0001)` —— 一个相对意义的浮点阈值。

而 [gesvj/README.md:74](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md#L74) 还交代了参考模型本身来自哪里：「该实现的精度已用 Lapack 的 `dgesvd`（基于 QR 的 SVD）与 `dgesvj`（Jacobi SVD）验证。」——也就是说，「参考」不一定是一份静态文件，也可以是权威 CPU 库的运行结果。

#### 4.2.4 代码实践

**实践目标**：对照三种范式的判定行，确认它们「容不容差、容哪种差」。

**操作步骤**：

1. 打开 `security/L1/benchmarks/crc32/host/main.cpp`，确认第 223 行用的是 `!=`（无容差）。
2. 打开 `dsp/L2/examples/vss_fft_ifft_1d/host.cpp`，确认第 244 行用的是 `err > level`（绝对容差 256）。
3. 打开 `solver/L2/benchmarks/gesvj/test_gesvj.cpp`，确认第 283 行用的是 `errA > 0.0001`（浮点范数容差）。

**需要观察的现象**：三段代码的判定算子从 `!=` → `> level` → `> 1e-4`，容差从「无」到「绝对整数」到「相对浮点」，正好对应运算类型从「位运算」到「定点数值」到「浮点分解」。

**预期结果**：你能用一句话说清「为什么 CRC 能 bit 精确、而 SVD 必须用范数」。

#### 4.2.5 小练习与答案

**练习 1**：假如把 DSP FFT 校验里的 `int level = (1 << 8)` 改成 `int level = 0`，会发生什么？

**参考答案**：阈值变成 0，等价于要求 bit 精确。由于 FFT 内核是定标定点、参考模型不 bit 精确，几乎必然有样本的 `err_re` 或 `err_im` 不为 0，从而 `this_flag` 置位、整体判 `*** FAILED ***`。这正是为什么作者要留一个 256 的容差。

**练习 2**：SVD 校验为什么不直接比较 `U`、`S`、`V` 的元素，而要重构 `A_out` 再比？

**参考答案**：SVD 的奇异值符号、奇异向量方向存在数学等价的多解（如 `U` 某列取反、对应 `V` 列也取反，乘积 \(U\Sigma V^T\) 不变）。直接比元素会因为这些合法差异误判失败；重构 \(A_{\text{out}}\) 后再比，把「多解」吸收进乘积里，比较的才是真正想要的「分解是否还原了原矩阵」。

---

### 4.3 误差阈值与判定

#### 4.3.1 概念说明

上一节讲了「用哪种范式比」，这一节聚焦范式 2/3 里那个魔法数字：**阈值（threshold）从哪来、怎么定、判定的逻辑怎么写**。

工程上定阈值没有银弹，但有两条通用原则：

1. **阈值要与误差的度量同量纲**。绝对误差配绝对阈值、相对误差配相对阈值，错配会导致大数放过、小数误杀。
2. **阈值要反映「业务上可接受的精度损失」**。比如通信 FFT 关心 SNR（信噪比），就该用相对误差或 SNR 阈值；图像处理关心像素差异，用绝对阈值即可。

数值类内核的相对误差通常定义为：

\[ e_{\text{rel}} = \frac{|x_{\text{ref}} - x_{\text{actual}}|}{|x_{\text{ref}}|} \]

而整批输出的整体误差常用 L2 范数或 SNR：

\[ \text{SNR}_{\text{dB}} = 10 \log_{10}\!\left( \frac{\sum |x_{\text{ref}}|^2}{\sum |x_{\text{ref}} - x_{\text{actual}}|^2} \right) \]

加速库里常见的做法是简化成绝对阈值（DSP）或 L2 范数（solver），本质都是在「不 bit 精确」的前提下，给一个可判定的边界。

#### 4.3.2 核心流程

PASS/FAIL 的判定流程，在本仓库里几乎都是同一种「累加失败标志」骨架：

```
flag = 0
for 每个输出样本:
    读参考 ref
    读实际 actual
    err = 度量(ref, actual)
    if err 超阈值:
        this_flag = 1     # 标记本样本失败
    flag |= this_flag     # 累加到全局
if flag == 0:  PASSED
else:          FAILED
```

这套骨架的好处是：**任何一个样本失败都会让最终 `flag != 0`**，是「最严」的聚合策略（与「平均误差超阈值才失败」相对）。它牺牲了对个别离群点的容忍，换来对「整体一致性」的强保证。

#### 4.3.3 源码精读

回到 DSP FFT 的判定。上一节已展示 `this_flag` 与 `flag |= this_flag`，这里补全它的**最终 PASS/FAIL 输出**：

- [host.cpp:257-261](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L257-L261)：

```cpp
if (flag == 0)
    std::cout << std::endl << "--- PASSED ---" << std::endl;
else
    std::cout << std::endl << "*** FAILED ***" << std::endl;
return (flag);
```

注意两点：

1. 返回值就是 `flag`——进程退出码非 0 即失败。这是给 CI 的契约：机器只需看退出码，不用解析 stdout（u14-l1 讲过这套「退出码契约」）。
2. [host.cpp:245-248](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L245-L248) 会在每个样本行打印 `Gld ... Act ... Err ...`，失败的样本额外打 `***`，方便人工定位是哪个频率点漂移过大。

CRC32 的判定则用 `nerr` 计数（[main.cpp:228-233](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L228-L233)），并通过 `utils_sw::Logger` 统一输出 `Test passed` / `Test failed`（Logger 在 [logger.hpp:210-215](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_sw/logger.hpp#L210-L215) 里把枚举映射成文案）。solver 同样走 Logger（[test_gesvj.cpp:284-287](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp#L284-L287)）。所以全仓库的 PASS/FAIL 文案其实是统一的：要么是 DSP 这种自打印字符串，要么是经 `Logger` 打印的 `Test passed/failed`，且都以进程退出码收尾。

#### 4.3.4 代码实践

**实践目标**：动手改变阈值，观察判定边界的变化（源码阅读 + 参数实验）。

**操作步骤**：

1. 阅读 `dsp/L2/examples/vss_fft_ifft_1d/host.cpp` 第 234、244、257-261 行，确认阈值 `level=256` 与退出码的关系。
2. 假设性实验（不必真的上板）：把第 234 行改成 `int level = (1 << 12);`（即 4096），推演会发生什么。

**需要观察的现象 / 预期结果**：阈值放大 16 倍后，原本被 `***` 标记的离群样本会被「放过」，`flag` 更可能保持 0，从而更易判 PASSED。这揭示了阈值的本质：**它是工程上对「可接受误差」的人为约定，调大就是把尺子放宽**。

**待本地验证**：上板跑 `make run TARGET=hw` 后，用 `echo $?` 看退出码，对比不同阈值下的 PASS/FAIL。

#### 4.3.5 小练习与答案

**练习 1**：DSP FFT 的 `flag |= this_flag` 用「按位或」聚合，如果改成「失败样本数 / 总样本数 > 5% 才判失败」，会有什么不同的工程含义？

**参考答案**：前者是「零容忍」——任何一个样本超阈值即整体失败，适合对一致性要求高的场景；后者是「统计容忍」——允许少数离群点，适合离群点无实际危害（如个别高频分量误差略大但不影响整体信号）的场景。两者没有绝对优劣，取决于业务对精度的敏感度。

**练习 2**：为什么 CRC32 的判定不需要 `level` 这种阈值？

**参考答案**：CRC 是确定性整数运算，给定输入其输出唯一，硬件实现与软件参考必然 bit 相等。没有「数值误差」这个概念，自然不需要容差——`!=` 已经是最精确且唯一正确的判据。

---

### 4.4 性能指标解读

#### 4.4.1 概念说明

正确性解决了「能不能用」，性能指标回答「值不值得用」。加速库的 benchmark 通常报告四类指标，理解它们的物理含义是解读任何 Profiling 表的前提：

| 指标 | 含义 | 单位 | 决定什么 |
|------|------|------|----------|
| **频率 Frequency** | 内核运行时钟 | MHz | 单位时间能做多少拍计算 |
| **延迟 Latency / Kernel time** | 处理一批数据耗时 | ms | 单次响应快慢 |
| **吞吐 Throughput** | 单位时间处理的数据量 | GB/s、GOPS、GFlops | 稳态搬运/算力 |
| **资源 Resource** | 占用的硬件面积 | LUT/REG/BRAM/URAM/DSP | 能否装下、成本 |

四者互相牵制：提高频率可能让时序收敛不了；加大并行度（更多 DSP/URAM）能提升吞吐但也吃面积；延迟与吞吐在流水线下可以解耦（稳态吞吐由最慢 stage 决定，与单次延迟无关——见 u12-l1）。

吞吐的基本定义：

\[ \text{Throughput} = \frac{\text{有效数据量 (bytes)}}{\text{kernel 执行时间 (s)}} \]

例如 CRC32 处理 256 MB 用 54.6 ms，则吞吐约为 \(256 \times 10^6 / 54.6\times10^{-3} \approx 4.7\) GB/s——这正是 README 表里的数字。

#### 4.4.2 核心流程

测时有两种手段，精度与含义不同，**极易混淆**：

1. **主机端 wall-clock**：在主机上用 `gettimeofday` 裹住 `enqueueTask + finish`，测的是「主机视角的总耗时」，含启动、DMA 搬运、内核执行、同步全部开销。简单但偏大。
2. **设备端 profiling**：开启 `CL_QUEUE_PROFILING_ENABLE`，用 OpenCL event 的 `CL_PROFILING_COMMAND_START/END` 取**设备上**某阶段的精确起止时间（纳秒级），能拆出 Write DDR / Kernel / Read DDR 三段。

加速库的 benchmark 通常**两种都打**：wall-clock 给整体直觉，profiling 给瓶颈定位。

#### 4.4.3 源码精读

**两种计时手段并存（CRC32）**

[security/L1/benchmarks/crc32/host/main.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp) 同时演示了两种手段。计时辅助函数 `tvdiff` 定义在 [utils.hpp:19-21](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/utils.hpp#L19-L21)，把两个 `timeval` 之差折算成微秒。

主机端 wall-clock（注意命令队列在 [main.cpp:136](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L136) 创建时带了 `CL_QUEUE_PROFILING_ENABLE`）：

- [main.cpp:185-201](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L185-L201)：`gettimeofday(&start_time)` → `q.finish()` → `gettimeofday(&end_time)` → 打印 `Execution time ... ms`。

设备端 profiling（拆三段）：

- [main.cpp:203-218](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L203-L218)：分别取 `events_write` / `events_kernel` / `events_read` 的起止时间，打印：

```
Write DDR Execution time ... ms
Kernel Execution time ... ms
Read DDR Execution time ... ms
Total Execution time ... ms
```

这套输出直接对应 [README.md:52-57](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/README.md#L52-L57) 给出的示例日志，从中能一眼看出瓶颈在 Kernel 段（~40 ms）而非 DMA 搬运（~1 ms）。

**Profiling 表（CRC32）**

[README.md:66-70](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/README.md#L66-L70) 给出最终归档的性能表：

```
| Frequency |   LUT  |   REG  | BRAM | URAM | DSP | Throughput |
| 300 MHz   | 5,322  | 10,547 |  16  |   0  |  0  |  4.7 GB/s  |
```

读法：在 U50 上跑到 300 MHz，用了 5322 个 LUT（面积很小，因为 CRC 是位运算不用 DSP/URAM），吞吐 4.7 GB/s。这张表是 benchmark 的「最终交付物」——把上面的运行日志凝练成一行可对比的数字。

**多配置对比表（solver GESVJ）**

`solver` 的 SVD 基准更进一步，给出**不同矩阵规模与展开因子**的对比表。[gesvj/README.md:95-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md#L95-L103) 列了 64×64（Unroll=2）与 512×512（Unroll=4/16）三档，每档给出 URAM/BRAM/DSP/Reg/LUT、Kernel time、Frequency。注意 512×512 Unroll=16 时 DSP 涨到 1808、Kernel time 4686.5 ms——这种「同内核、不同配置」的对比表，能帮读者看清「面积换时间」的具体斜率。

solver 的 wall-clock 计时逻辑在 [test_gesvj.cpp:211-224](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp#L211-L224)，`diff()` 函数在 [test_gesvj.cpp:38-41](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/test_gesvj.cpp#L38-L41)，用 `num_runs` 次取平均降低抖动。

#### 4.4.4 代码实践

**实践目标**：把一段运行日志换算成吞吐指标，验证 README 的数字。

**操作步骤**：

1. 读 `security/L1/benchmarks/crc32/README.md` 第 4 行确认输入是 256 MB（\(256 \times 10^6\) 字节）。
2. 读第 52-57 行示例日志，取 `Kernel Execution time` ≈ 40.8 ms 与 `Total Execution time` ≈ 42.2 ms。
3. 用吞吐公式算：\(256\times10^6 / (40.8\times10^{-3}) \approx 6.27\) GB/s（仅 kernel 段），\(256\times10^6 / (42.2\times10^{-3}) \approx 6.06\) GB/s（含搬运）。

**需要观察的现象**：你算出的数比 README 表里的 4.7 GB/s 偏大。这说明两点：(a) README 的 4.7 GB/s 是按更长/更稳态的实测时间取的，(b) wall-clock vs profiling、单次 vs 稳态会给出不同数字——所以对比性能时**必须说清度量口径**。

**预期结果**：理解「同样是吞吐，不同计时口径会给出不同值」，今后读 Profiling 表会先看它的口径定义。

**待本地验证**：在 U50 上用 `-num 16`（[README.md:36](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/README.md#L36)）跑多次，取稳态时间再换算。

#### 4.4.5 小练习与答案

**练习 1**：CRC32 基准里「Execution time（gettimeofday）」与「Kernel Execution time（profiling event）」哪个更大？为什么？

**参考答案**：通常 `Execution time`（主机 wall-clock）更大，因为它包含了 OpenCL 命令入队、`finish()` 同步等主机侧开销；而 `Kernel Execution time` 只量设备上内核真正执行的纳秒区间。两者之差就是「主机与 PCI 交互」的开销。

**练习 2**：solver GESVJ 的表里，矩阵从 512×512 Unroll=4 到 Unroll=16，DSP 从 500 涨到 1808、Kernel time 从 4827 ms 降到 4686.5 ms。这反映了什么权衡？

**参考答案**：展开因子（Unroll）增加近 4 倍，DSP 也涨了约 3.6 倍（500→1808），但 Kernel time 只略微下降（4827→4686.5）。这说明此时瓶颈可能不在算力并行度，而在访存或迭代收敛次数；继续堆并行度的边际收益已经很小，是典型的「面积换时间」收益递减区。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串起来，给 `dsp/L2/examples/vss_fft_ifft_1d` 写一份「微型基准评测说明」。

请按以下步骤完成（纯源码阅读 + 推演，无需上板）：

1. **定位参考模型**：打开 [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp)，找到加载 `ref_output.txt` 的行（第 220 行），并说明参考输出是从主机侧文本文件读入的——它代表「另一个模型（很可能是高精度软件 FFT）预先算好的结果」。
2. **还原判定逻辑**：阅读第 233-261 行，说明它如何用 `level = (1 << 8)` 这个**绝对误差阈值**做范式 2 判定，并用 `flag |= this_flag` 做零容忍聚合，最后以进程退出码 `return (flag)` 给 CI。
3. **解释非 bit 精确**：用自己的话写出三条原因——(a) AIE/PL 的 FFT 内核用定标定点（`cint32` 整数表示），参考模型不 bit 精确；(b) 蝶形运算的加法顺序在硬件与软件里不同，浮点/定点非结合律导致末位差异；(c) 量化舍入在每一级都会累积。
4. **补一个性能口径**：注意 DSP 这个示例的 host.cpp **没有**打印 Execution time 或 throughput（它是个功能正确性示例，不是完整 benchmark）。请对照 CRC32 的 [main.cpp:185-218](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/L1/benchmarks/crc32/host/main.cpp#L185-L218)，写一段说明：若要把它升级成 benchmark，需要补哪些计时代码（wall-clock `gettimeofday` + profiling event）、以及为什么必须同时报告数据量与 kernel time 才能算出吞吐。

**交付物**：一段不超过 300 字的中文说明，覆盖「参考来源 / 阈值与判定 / 为何非 bit 精确 / 若做性能报告需补什么」四点。

**预期结果**：你能清楚区分「这个示例只验证正确性」与「一个完整 benchmark 还需要性能度量」，并理解两者共用同一套主机控制链、差别只在「是否计时与报数据量」。

## 6. 本讲小结

- **benchmarks 目录**与 tests 共用 `description.json` 契约（`flow`/`category`/`targets` 完全一致），差别在用大数据压满带宽、并在 README 里归档 Profiling 表；分布在 `solver/L2`、`security/L1`、`vision/L3`、`dsp/L2` 等位置。
- **参考比对有三种范式**：CRC 用 bit 精确（`!=`）、DSP FFT 用绝对误差阈值（`level=256`）、solver SVD 用 L2 范数阈值（`errA>1e-4`），范式选择由运算的数值性质决定。
- **数值内核做不到 bit 精确**，根源是定点 vs 浮点、浮点非结合律、迭代算法的合法多解；因此阈值是「工程上对可接受精度损失的人为约定」。
- **PASS/FAIL 判定**统一采用「累加失败标志 + 进程退出码」骨架，机器只看退出码，人工看 stdout 的逐样本日志。
- **性能指标四件套**：频率、延迟、吞吐、资源；测时有主机 wall-clock（`gettimeofday`）与设备 profiling（OpenCL event）两种口径，对比数字前必须先对齐口径。
- **benchmark 的最终交付物**是 README 里那张 Profiling 表，它把运行日志凝练成可跨平台对比的一行数字。

## 7. 下一步学习建议

- 想亲手造一个完整 benchmark？回到 u14-l2，把它教的「从零写 L1 内核 + testbench + Makefile + description.json」补上本讲的「wall-clock + profiling 计时 + 大数据输入」，就是一个最小可用的基准。
- 想深入理解「为什么定点 FFT 的误差长那样」？阅读 u6-l1（L1 HLS FFT）里 `ssr_fft_default_params` 的输入输出位宽与定标，把本讲的 `level=256` 与 FFT 的定点格式对应起来。
- 想看清「面积换时间」的极限？结合 u12-l1（dataflow/SSR/datawidth/II）与 u12-l2（URAM/HBM banking）的性能旋钮，对照 solver GESVJ 的多配置对比表，体会边际收益递减的拐点。
- 想做跨架构对比？阅读 `vision/L3/benchmarks/`（colordetect 等）的 README，它们正是「与其它架构性能对比」的范本，是本讲理念在应用层的落地。
