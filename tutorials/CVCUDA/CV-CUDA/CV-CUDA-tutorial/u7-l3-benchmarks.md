# 基准测试：nvbench C++ 基准与 Python 基准

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `bench/` 目录的双语言基准体系是如何组织的：C++（nvbench）与 Python（cuda-python 的 `cuda.bench`）两套基准逐算子成对出现，由同一个配置驱动。
2. 用 `run_bench.py` 运行基准、读懂输出的 GPU Time / GPU Noise / BWUtil / Py overhead 等指标，并理解噪声门禁与 C++/Python 性能对齐（parity）检查。
3. 解释 `bench/config` 共享配置体系：`bench_params.json` 清单 → `operators/*.json` 算子文件 → 自动生成的 C++ 配置头 → 内嵌 SKU 基线。
4. 解释 `WarmupPolicy.hpp`（及 Python 侧 `_internal/warmup.py`）的预热策略：预热为什么存在、`CVCUDA_BENCH_WARMUP_CAP` 如何在两种语言里统一封顶。
5. 用 `compare_to_baseline.py` 把一次运行的 JSON 结果与仓库内提交的 A100/H100 基线做性能回归对比，读懂五类比对结果与退出码。

本讲是质量保障单元（第七单元）的第三讲。u7-l1 讲了「算子算得对不对」（系统测试），本讲讲「算子跑得快不快、快慢是否稳定」（基准与回归门禁）。

## 2. 前置知识

### 2.1 什么是基准（benchmark），什么是 nvbench

基准是「在受控条件下反复测量一段代码的耗时」的工具。GPU 基准的难点在于测量值天然不稳定：GPU 频率会动态升降、缓存有冷热、首次调用要做惰性初始化。nvbench 是 NVIDIA 开源的 C++ 基准框架，核心思路是：

- **轴（axes）**：把影响性能的参数（形状、dtype、插值方式）声明成坐标轴，框架自动对所有组合各测一次。
- **CUDA 事件计时**：用事件而不是 CPU 时钟计时，测的是纯 GPU 时间。
- **噪声（noise）**：多次重复测量后报告相对标准差，告诉你这个数字可信到什么程度。

CV-CUDA 的 Python 基准没有直接用 nvbench 的 C++ API，而是用 `cuda-python` 包提供的 `cuda.bench` 模块——它把 nvbench 的轴/计时模型以几乎相同的接口暴露给 Python，这样同一份配置可以同时驱动 C++ 与 Python 两侧（详见 `bench/python/python_bench_utils.py` 中的 `run_benchmark`，它调用 `cuda.bench.register` 与 `bench.run_all_benchmarks`）。

### 2.2 性能对齐（parity）检查

CV-CUDA 的 Python 绑定只是 C++ 的薄包装（见 u5-l1 的四层结构），因此同一条算子调用在两种语言下的 GPU 时间应当几乎相同。基准体系利用这一点做交叉验证：如果 Python 侧比 C++ 侧慢得超出阈值，说明绑定层引入了额外开销（比如误用了当前流、缓存未命中），这往往是 bug 而不是「Python 本来就慢」。

### 2.3 SKU 与基线（baseline）

同一个算子在不同 GPU 上耗时不同，因此基线必须绑定到具体的 GPU 型号。仓库用「SKU」（库存单元，这里指一种 GPU 身份，如 `H100_PCIe_350W_1095MHz`）标记一组参考机器，把在那些机器上测得的时间直接提交进 `bench/config/operators/*.json`，作为性能回归的对照标准。

### 2.4 与前面讲义的衔接

- u7-l1 的五段式测试范式（随机数据→金标→GPU→比对）校验**正确性**；本讲的范式是（配置→预热→计时→噪声/对齐门禁→基线比对）校验**性能**。
- u4-l1 的流模型在本讲反复出现：Python 基准必须把 kernel 提交到 nvbench 正在计时的那条流上，否则测出来的是假时间。
- u4-l2 的对象缓存在本讲有一个直接应用：基准框架在计时前后显式调用 `cvcuda.clear_cache()`，确保计时区间不包含缓存清空带来的抖动。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [bench/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md) | 基准体系权威文档：三大工作流、目录结构、配置字段参考 |
| [bench/run_bench.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py) | 总入口：驱动 C++/Python 基准、锁 SM 时钟、做噪声与对齐验证、输出 CSV/JSON |
| [bench/config/bench_params.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/bench_params.json) | 算子清单（manifest）：每个算子指向其配置文件与两侧基准脚本 |
| [bench/config/operators/resize.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/resize.json) | 单算子配置：config_key、tier、轴、warmup_iterations 与内嵌基线 |
| [bench/config/sku_map.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/sku_map.json) | 受支持的基准 GPU 身份表 |
| [bench/config/load_config.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/load_config.py) | Python 侧加载算子配置的模块 |
| [bench/python/ops/bench_resize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py) | resize 的 Python 基准（本讲解剖样本） |
| [bench/python/python_bench_utils.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py) | Python 基准共享框架：配置注册、流缓存、预热、执行入口 |
| [bench/python/perf_utils.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/perf_utils.py) | **样例级** NVTX 计时工具（CvCudaPerf），与 ops 基准是两套体系 |
| [bench/cpp/ops/BenchResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp) | resize 的 C++ 基准（与 Python 版逐行对照） |
| [bench/cpp/CppBenchUtils.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/CppBenchUtils.hpp) | C++ 基准共享工具：warmup / exec_with_sync / warmup_and_exec |
| [bench/cpp/WarmupPolicy.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/WarmupPolicy.hpp) | C++ 预热封顶策略（环境变量解析） |
| [bench/_internal/warmup.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/warmup.py) | Python 侧同一策略的镜像实现 |
| [bench/_internal/quality.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/quality.py) | 质量阈值单一来源：噪声、相对/绝对对齐门禁 |
| [bench/cpp/GenerateBenchConfig.cmake](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/GenerateBenchConfig.cmake) | 构建期从算子 JSON 生成 C++ 配置头 |
| [bench/compare_to_baseline.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py) | 基线回归对比工具（本讲第五模块主角） |

---

## 4. 核心概念与源码讲解

### 4.1 模块一：基准体系总览——双语言基准、三大工作流与质量门禁

#### 4.1.1 概念说明

`bench/` 目录回答一个问题：**每个算子在真实工作负载下跑多快，且这个数字可不可信**。它由三条工作流组成：

| 目标 | 命令 | 主输出 |
|---|---|---|
| 运行 C++ 和/或 Python 基准 | `run_bench.py` | `bench_output.csv` 或 JSON |
| 对比两个 Python wheel | `compare_wheels.py` | `summary.md` 与 `comparison.csv` |
| 与仓库内提交基线对比 | `compare_to_baseline.py` | 控制台 / Markdown / JUnit 报告 |

这三条工作流记录在 [bench/README.md:L14-L22](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L14-L22)，README 开头同时说明基准报告三类指标：GPU 时间、测量噪声、带宽利用率，并且「成对的 C++ 与 Python 运行还检查两语言性能是否一致」，仓库为 A100 与 H100 提交了基线（[bench/README.md:L8-L12](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L8-L12)）。

目录组织是「一算子、双镜像」：`bench/cpp/ops/BenchXxx.cpp` 与 `bench/python/ops/bench_xxx.py` 逐算子成对，共同吃同一份 `bench/config/operators/xxx.json` 配置，如目录结构图所示（[bench/README.md:L255-L267](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L255-L267)）。

#### 4.1.2 核心流程

一次典型基准运行的全流程：

```text
run_bench.py 启动
  ├─ 1. 探测 GPU、尝试锁定 SM 时钟（消除频率漂移噪声）+ 启动时钟采样线程
  ├─ 2. 读 bench_params.json 清单，按 --operator/--tier/--config-key 过滤
  ├─ 3. 对每个选中算子：
  │     ├─ 运行 C++ 可执行文件 bench_<op>（由配置 JSON 生成的轴头文件驱动）
  │     └─ 运行 Python 脚本 bench_<op>.py（由 load_config.py 读取同一 JSON）
  ├─ 4. 汇总两侧结果为 DataFrame：
  │     ├─ 计算噪声绝对值（µs）
  │     ├─ 为成对配置计算 Py overhead（% 与 µs）
  │     └─ 逐行写 Status：噪声超标或对齐超标 → FAIL
  ├─ 5. validate：噪声门禁 + 配置成对性 + 性能对齐门禁
  └─ 6. 写 bench_output.csv（或 --output 指定的 JSON）
退出码：0 成功 / 1 执行或验证失败 / 2 参数或配置错误
```

#### 4.1.3 源码精读

**（1）为什么先锁 SM 时钟**。`run_bench.py` 开头有一段难得的「机制说明注释」：作者在 K8s GPU 池上观察到部分节点的 SM 时钟没有升频，同一 SHA + 同一 SKU 标签下整波基准慢 40–75%；在基准开始前把 SM 时钟锁定到固定值，可以消除频率漂移这一噪声源，让构建间对比具有确定性（[bench/run_bench.py:L55-L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L55-L69)）。锁定目标时钟默认取 `1095,1005,900,750` 的优先列表，与设备实际支持的时钟求交集后取最大值（[bench/run_bench.py:L71-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L71-L83)）。1095 正是 A100 PCIe 40GB 与 H100 PCIe 的标称基础 SM 时钟——这也是 SKU 名里 `1095MHz` 后缀的由来。锁定需要 root/CAP_SYS_ADMIN 权限，被拒绝时会优雅降级，同时后台线程每 0.5 秒采样实时时钟、功耗、温度写入 JSONL 日志，供事后解释时间漂移。

**（2）输出表有哪些列**。结果表的列定义分五类：配置列（各轴取值）、度量列、派生列、元数据列。度量列是解读输出的关键（[bench/run_bench.py:L395-L407](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L395-L407)）：

- `GPU Time (µs)` / `GPU Noise (%)` / `GPU Noise (µs)`：GPU 时间与噪声（噪声绝对值由前两者换算，见 [bench/run_bench.py:L763-L768](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L763-L768)）。
- `CPU Time` 系列：宿主侧耗时。
- `GlobalMem BW (bytes/sec)` 与 `BWUtil`：带宽利用率——因为多数视觉算子是访存受限的，BWUtil 越接近 1 说明 kernel 越接近硬件极限。
- `Py overhead (%)` / `Py overhead (µs)`（派生）：Python 相对 C++ 的开销。
- `Status`（派生）：PASS/FAIL 及原因。

**（3）质量阈值的单一来源**。噪声与对齐的判定不是散落在各处的魔数，而是集中在 `_internal/quality.py` 的 `DEFAULT_BENCHMARK_QUALITY`：噪声上限 10%、相对对齐上限 10%、绝对对齐上限 100µs（[bench/_internal/quality.py:L68-L72](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/quality.py#L68-L72)）。该文件 docstring 明确要求运行时验证、提交基线校验与基线导入三处都必须读这个模块，「让质量策略在采集与持久化之间不发生漂移」。对齐判定同时检查相对差和绝对差（[bench/_internal/quality.py:L62-L65](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/quality.py#L62-L65)）：相对阈值管比例、绝对阈值兜底「很快的算子比例容易虚高」的场景。

`ResultsProcessor._compute_status_and_parity` 把阈值落到每一行：为成对配置取两侧的 GPU 时间与噪声，任一噪声超标或对齐超标就向失败原因列表追加条目（[bench/run_bench.py:L775-L846](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L775-L846)）。README 中示例输出的最后一行 `✓ All validations passed (noise <10.0%, perf diff <10.0% AND <100us)` 正是这三个默认值的体现（[bench/README.md:L92-L99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L92-L99)）。

**（4）运行方式速查**。从构建目录出发的常用命令（[bench/README.md:L48-L64](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L48-L64)）：无参数跑 basic 档的双语言基准；`--operator resize,gaussian` 选算子；`--lang python` 选语言；`--tier basic,advanced` 加深档位；`--config-key` 精确到一条配置；`--list-operators` 发现合法算子名。默认输出 CSV，要与基线比对时必须输出 JSON（[bench/README.md:L69-L74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L69-L74)）。`--skip-validation` 保留各行 Status 但不让门禁失败拖垮整次运行。

#### 4.1.4 代码实践

1. **实践目标**：跑通一次最小基准并逐列解读输出。
2. **操作步骤**：
   - 在有 CUDA 12/13 环境的机器上，按 u1-l3 构建 bench（或确认 `build-rel/bin` 下存在 `run_bench.py` 与 `bench_resize`）；
   - 执行 `cd build-rel/bin && python3 run_bench.py --lang python --config-key resize_contract_area_tensor_uchar3_basic --output run1.json`；
   - 再执行一次，输出 `run2.json`；
   - 用 `python3 -c "import pandas as pd; print(pd.read_json('run1.json').to_string())"` 或直接打开文件查看各字段。
3. **需要观察的现象**：两次运行的 `gpu_time_us` 是否几乎一致；`gpu_noise_us` 相对时间的百分比有多大；`bwutil` 是多少。
4. **预期结果**：在锁频成功的机器上，两次同 config_key 的 GPU 时间差应在个位数百分比内，噪声远低于 10% 门禁。若机器不在 SKU 列表中，输出里记录的是本机的原始测量，不能与 A100/H100 基线直接对比。本实践依赖 GPU 与构建产物，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run_bench.py` 要在测前锁定 SM 时钟，而不是让 GPU 自由升频到最高？
**答案**：自由升频下，测得的耗时混入了「当时频率是多少」这一不可控变量。README/注释中给出的实际教训是：同一代码在部分节点上因时钟未升频而整波慢 40–75%。锁定到固定频率（如 1095MHz）后，频率不再是噪声源，构建与构建之间的对比才有意义——基准追求的是**可复现**，不是绝对最快。

**练习 2**：`Py overhead` 一列是什么？为什么它大到一个程度会被当作失败？
**答案**：它是同一 config_key 下 Python 基准相对 C++ 基准的时间差（% 与 µs 两种口径，计算见 [bench/_internal/quality.py:L45-L51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/quality.py#L45-L51)）。因为 Python 绑定只是 C++ 的薄包装，两侧 GPU kernel 完全相同，开销理应接近零；超出 10% 或 100µs 通常意味着绑定层有额外工作（例如提交到了错误的流），属于实现缺陷而非语言固有开销。

**练习 3**：退出码 1 和 2 分别代表什么？
**答案**：1 表示基准执行或验证失败（含噪声超标、对齐超标）；2 表示命令行参数或配置文件本身非法。见 [bench/README.md:L102-L103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L102-L103)。CI 可以据此区分「代码变慢了」与「基准没写对」。

---

### 4.2 模块二：bench/config 共享配置——一份 JSON 驱动两种语言

#### 4.2.1 概念说明

基准最大的工程陷阱是「C++ 测的东西和 Python 测的东西悄悄不一样」。CV-CUDA 的解法是把**所有**性能相关的选择（哪些形状、哪些 dtype、预热多少次、基线是多少）集中到 `bench/config/`，两侧基准只做「执行配置」的哑机器：

- `bench_params.json`：算子清单，登记每个算子的配置文件路径与两侧基准目标名（[bench/config/bench_params.json:L2-L12](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/bench_params.json#L2-L12) 中 `adaptivethreshold` 的条目即是典型：`config` 指向 `operators/adaptivethreshold.json`，`cpp` 是 `bench_adaptivethreshold` 可执行目标，`python` 是 `bench_adaptivethreshold.py` 脚本）。
- `operators/<op>.json`：单算子配置。每个条目以稳定的 `config_key` 为键，包含 tier（basic/advanced 两档，basic 默认跑）、dtypes、三类轴（`string_axes`/`int64_axes`/`float64_axes`）、`warmup_iterations`，以及机器生成的 `baselines` 基线块。
- `sku_map.json`：受支持的基准 GPU 身份表（[bench/config/sku_map.json:L2-L16](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/sku_map.json#L2-L16)）：每条记录 GPU 名称、功率上限、锁定 SM 时钟与 stem（即基线里的键，如 `H100_PCIe_350W_1095MHz`）。
- `load_config.py`：Python 侧配置加载器，例如把 `warmup_iterations` 读出并默认 100（[bench/config/load_config.py:L392](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/load_config.py#L392)）。

C++ 侧不能直接执行期读 JSON，因此构建期由 [bench/cpp/GenerateBenchConfig.cmake](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/GenerateBenchConfig.cmake) 把 JSON 翻译成头文件：类型轴合成 `BENCH_<OP>_TYPES`，轴值合成 `BENCH_<OP>_AXES`，`warmup_iterations` 取「首个设置该值的配置」且缺省为 0（[bench/cpp/GenerateBenchConfig.cmake:L105-L109](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/GenerateBenchConfig.cmake#L105-L109)、[L283-L285](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/GenerateBenchConfig.cmake#L283-L285)），最终生成宏 `#define BENCH_RESIZE_WARMUP_ITERATIONS ...`（[bench/cpp/GenerateBenchConfig.cmake:L326-L327](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/GenerateBenchConfig.cmake#L326-L327)）。README 对此的表述是「值的变化不需要重新构建；新增轴名、dtype 或算子时才需要重建对应的 `bench_<op>` 目标」（[bench/README.md:L364-L371](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L364-L371)）。

#### 4.2.2 核心流程

配置从 JSON 到两侧基准的数据流：

```text
bench/config/bench_params.json（清单）
        │ 指向
        ▼
bench/config/operators/resize.json
        │                                    │
        │ 构建期 GenerateBenchConfig.cmake    │ 运行期 load_config.py
        ▼                                    ▼
cpp/ops/generated/BenchResizeConfig.hpp   python_bench_utils.load_operator_config
（宏：类型轴/轴值/预热次数）                （dtypes、string_axes、warmup_iterations）
        │                                    │
        ▼                                    ▼
BenchResize.cpp 的 NVBENCH_BENCH_TYPES    bench_resize.py 的 b.add_string_axis
与 BENCH_RESIZE_AXES                      与 register_axes_from_config
```

基线块的键是「完全展开的用例身份」：`config_key[轴名=值][轴名=值]...`，值下再按 SKU 记录 `n_runs`（导入的运行次数）、两侧 GPU 时间与噪声、带宽利用率，以及可选的跨运行 C++/Python 间隔标准差 `gpu_gap_stddev_us`。字段含义见 README 的字段参考表（[bench/README.md:L341-L359](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L341-L359)）。

#### 4.2.3 源码精读

以 resize 为例看一个完整的配置条目。`resize_contract_area_tensor_uchar3_basic` 声明：tier 为 basic、dtype 为 uchar3、形状 `32x1080x1920`（32 张 1080p 图的批）、缩小模式 CONTRACT、AREA 插值、NHWC 布局、Tensor 输入容器、预热 200 次（[bench/config/operators/resize.json:L4-L26](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/resize.json#L4-L26)）。同一条目下的 `baselines` 块按展开后的 case 键记录了 A100 与 H100 各自的测量：例如 A100 上 C++ 201.13µs / Python 210.96µs、噪声分别约 1µs 与 2.9µs、带宽利用率约 0.80 与 0.76（[bench/config/operators/resize.json:L30-L52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/resize.json#L30-L52)）。注意两点设计：

1. **Python 比 C++ 略慢是预期内的**（约 4–5%），这正是对齐门禁设 10% 的依据；
2. **BWUtil≈0.8** 说明该配置下 resize 已用掉八成理论带宽，进一步优化空间有限——这是判断「该不该继续优化某个算子」的客观依据（呼应 u8-l4 的 optimize-op 流程）。

`inputKind` 轴选择输入容器：`Tensor` 用稠密张量，`VarShape` 用 `ImageBatchVarShape`（变长批），个别支持张量批 API 的算子还有 `TensorBatch`（[bench/README.md:L360-L363](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L360-L363)）。这使同一算子的固定批与变长批两条代码路径都能被基准覆盖——它们走的是不同的 priv 实现分支（见 u5-l2）。

README 同时强调：基线块是**生成数据**，除手术式修复外禁止手改，每次评审过的更新都要保留来源运行或 CI 链接；更新基线要走 `_internal/update_baseline.py`（先 `--dry-run`）与 `_internal/validate_baselines.py` 的导入-校验-复检流程（[bench/README.md:L376-L408](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L376-L408)）。

#### 4.2.4 代码实践

1. **实践目标**：不运行任何 GPU 代码，仅凭配置文件回答「某个算子测了什么」。
2. **操作步骤**：
   - 打开 `bench/config/bench_params.json`，数一数 `operators` 下登记了多少个算子；
   - 在 `bench/config/operators/gaussian.json` 中找出所有 tier 为 basic 的 config_key，记下它们的 shape、dtype、warmup_iterations；
   - 对任一 config_key，展开它的 baseline case 键（形如 `xxx[InOutDataType=...][shape=...]...`），对照轴列表逐段解释每个 `[轴=值]`；
   - 打开 `bench/cpp/ops/generated/`（若已构建）确认 `BenchGaussian*` 宏与 JSON 的对应，未构建则阅读 GenerateBenchConfig.cmake 推导。
3. **需要观察的现象**：case 键中的轴排列顺序；同一 config_key 下 A100 与 H100 两组数字的比例关系。
4. **预期结果**：能仅凭 JSON 复述出「gaussian 的 basic 档在什么形状、什么 dtype、预热多少次、在两张参考卡上各花多少微秒」。此实践纯读文件，可立即完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么配置改动（比如把 shape 从 `1x1080x1920` 改成 `4x1080x1920`）不需要重新编译，而新增一个轴名（比如给 resize 加 `dstAlign` 轴）就需要？
**答案**：C++ 基准的轴是在**编译期**通过生成头文件里的宏（`BENCH_RESIZE_TYPES`、`BENCH_RESIZE_AXES`）注册进 nvbench 的；改已有轴的取值只影响传给可执行文件的运行期 `--axis` 过滤参数（README L364-L371 说明了 `run_bench.py` 运行期传值的机制），而新增轴名需要新的宏与新的 `state.get_*` 读取代码，必须重建 `bench_<op>` 目标。

**练习 2**：`tier` 字段的 basic 与 advanced 分别什么时候跑？
**答案**：basic 是默认档，`run_bench.py` 不加参数即运行；advanced 必须显式选择（`--tier basic,advanced` 或 `--config-key` 直达）。见 [bench/README.md:L57-L61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L57-L61) 与字段表 L349。这把「每次 CI 必测的代表性配置」与「深度覆盖矩阵」分开，控制常规运行时长。

**练习 3**：SKU 为什么把功率上限和锁定频率编进名字里？
**答案**：GPU 时间依赖运行条件。`A100_PCIE_40GB_250W_1095MHz` 声明了「40GB PCIe 版、250W 功率上限、SM 时钟锁定 1095MHz」这一受控条件；换一台功率上限不同或未锁频的同型号卡，测出的时间就不可比。SKU 名即测量条件的契约，对应 `sku_map.json` 中每条记录的三个属性字段。

---

### 4.3 模块三：Python 基准解剖——bench_resize.py 的三段式结构

#### 4.3.1 概念说明

每个 Python 基准脚本都是同一个模板的实例，结构分三段：

1. **setup 段**（benchmark 函数体）：从 `state` 读轴值，创建输入/输出容器，向框架申报本次运行读写多少全局内存（供 BWUtil 计算），最后返回一个 `run(launch)` 闭包。
2. **run 段**（返回的闭包）：只包含被测的那一次算子调用——这决定了「测的到底是什么」。
3. **注册段**（`__main__`）：调用共享的 `run_benchmark(operator_name, benchmark_func)`，由它完成配置加载、轴注册、预热与执行。

先辨析一个容易混淆的点：**`bench/python/perf_utils.py` 不属于本基准体系**。它是给 `samples/` 下的端到端样例（classification、object_detection 等）用的 NVTX 计时辅助类 `CvCudaPerf`——只负责压入/弹出 NVTX 范围并把占位的 `benchmark.json` 结构写盘，「实际计时数据要靠 NSYS 捕获后由外部脚本回填」，其 docstring 写明该类「没有真正做基准测量的功能，那由 NSYS 完成」（[bench/python/perf_utils.py:L41-L52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/perf_utils.py#L41-L52)、[L191-L200](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/perf_utils.py#L191-L200)）。它还提供 `maximize_clocks`/`reset_clocks`（用 nvidia-smi 锁频）与 `summarize_runs`（对比多次样例运行的 pandas 表）。两套体系一句话区分：**ops 基准测单个算子的 GPU 时间（nvbench 事件计时），perf_utils 测整条样例管线的端到端时间（NSYS + NVTX）**。perf_utils 的 NVTX 部分与 u7-l4 讲义直接相关，这里不展开。

#### 4.3.2 核心流程

`run_benchmark` 的完整生命周期（[bench/python/python_bench_utils.py:L481-L534](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L481-L534)）：

```text
run_benchmark("resize", resize)
  ├─ gc.disable()                      # 计时期间禁用自动 GC
  ├─ load_operator_config("resize")    # 读 operators/resize.json
  ├─ 包装成 wrapped_benchmark(state):
  │     ├─ cvcuda.clear_cache()        # 清对象缓存（u4-l2），首尾各一次
  │     ├─ run_fn = benchmark_func(state)   # setup 段在此执行
  │     ├─ run_warmup(run_fn, warmup_iterations)  # 预热（见模块四）
  │     └─ state.exec(run_fn, sync=True)     # 计时段
  │     └─ finally: cvcuda.clear_cache() + gc.collect()
  ├─ bench.register(wrapped_benchmark)  # 注册进 cuda.bench
  ├─ b.add_string_axis("InOutDataType", config.dtypes)
  ├─ register_axes_from_config(b, config)  # 其余轴来自 JSON
  └─ bench.run_all_benchmarks(bench_args)
```

三个防污染细节值得记住：禁用 GC、首尾清缓存、setup 与 teardown 收在同一个 state 局部作用域内——都是为了把「与被测算子无关的偶发停顿」挡在计时区间外。

#### 4.3.3 源码精读

**（1）setup 段：读轴、算字节数、建容器**。`resize(state)` 先从 state 取出全部轴值：形状串解析成 `(N,H,W)`、dtype 串映射成 `cvcuda.Type`、插值串映射成枚举、`inputKind` 决定容器类型（[bench/python/ops/bench_resize.py:L44-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L44-L55)）。形状串解析就是把 `"1x1080x1920"` 按 `x` 切开转整数（[bench/python/python_bench_utils.py:L102-L121](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L102-L121)）；输出形状由 `resizeType` 决定——`EXPAND` 翻倍、`CONTRACT` 减半、`TARGET_HxW` 精确指定（[bench/python/python_bench_utils.py:L124-L138](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L124-L138)）。

随后是 BWUtil 的关键：按 dtype 大小算出读写字节数并申报（[bench/python/ops/bench_resize.py:L75-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L75-L84)）：

```python
src_bytes = N * H * W * dtype_size
dst_bytes = N * dst_H * dst_W * dtype_size
state.add_global_memory_reads(src_bytes)
state.add_global_memory_writes(dst_bytes)
```

框架用 `实测时间 × 理论带宽` 与申报字节数比较得出 BWUtil。**如果申报不准，BWUtil 就是自欺**——例如把只读 1 字节的查找表算成读整图。这也解释了为什么融合算子基准（u3-l4）申报的字节数明显更小：中间结果只存在于寄存器。

容器创建按 `inputKind` 分两支：Tensor 分支按布局构造 `(N,H,W,C)` 或 `(N,C,H,W)` 形状的张量并填充棋盘格图案（[bench/python/ops/bench_resize.py:L112-L118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L112-L118)）；VarShape 分支构造 `ImageBatchVarShape`（[bench/python/ops/bench_resize.py:L119-L136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L119-L136)）。填充用棋盘格而非全零，是为了避免某些 kernel 对全零数据走「幸运的」缓存命中路径——数据要像真实数据。

**（2）run 段：被测的只有一行**。[bench/python/ops/bench_resize.py:L138-L142](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L138-L142)：

```python
def run(launch):
    stream = get_stream(launch)
    cvcuda.resize_into(dst, src, interp, stream=stream)
```

两个刻意选择：用 `_into` 变体（u3-l3 讲过：输出预分配，避免把对象缓存查找与分配算进被测时间）；流必须来自 `get_stream(launch)` 而不是随手用当前流——`create_stream_cache` 的 docstring 记录了一个真实踩过的坑：若测量阶段返回 `Stream.current`（通常是空流），kernel 提交到空流而 nvbench 在自己的专用测量流上打事件，「事件起止在空闲流上背靠背触发，测出一个虚假的极短时间」，数据依赖型算子（histogram、remap、sift）会以「Python 比 C++ 还快」的对齐失败形式暴露这个 bug（[bench/python/python_bench_utils.py:L374-L402](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L374-L402)）。因此闭包缓存了两条流：预热期（`launch is None`）用当前流，测量期永远用 `launch.get_stream()` 包装出的流（[bench/python/python_bench_utils.py:L403-L421](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L403-L421)）。这是 u4-l1「算子必须提交到被计时的流」的最直接应用。

**（3）C++ 镜像逐行对照**。`BenchResize.cpp` 与 Python 版结构完全同构：同样读轴、同样跳过不支持的组合（`state.skip`）、同样按真假平面分支申报字节数（[bench/cpp/ops/BenchResize.cpp:L30-L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp#L30-L69)）；Tensor 路径创建 `nvcv::Tensor` 并以同样的棋盘格填充，然后一行完成预热加执行（[bench/cpp/ops/BenchResize.cpp:L96-L113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp#L96-L113)）；VarShape 路径换成 `ImageBatchVarShape`（[bench/cpp/ops/BenchResize.cpp:L114-L136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp#L114-L136)）；文件末尾用生成的宏注册基准与轴（[bench/cpp/ops/BenchResize.cpp:L142-L145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp#L142-L145)）。两侧还共享一个「NCHW_FAKE」对比路径：平面数据先 reformat 成交错、用交错 kernel 缩放、再 reformat 回平面，全程计时，作为原生 NCHW 路径（NCHW 轴）的对照组（[bench/cpp/ops/BenchResize.cpp:L46-L54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp#L46-L54) 的注释；Python 侧对应 [bench/python/ops/bench_resize.py:L88-L110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L88-L110)）。这是「用基准证明一条优化路径更快」的范例：u5-l4 提过 OSD 的 planar 桥正是这种「reformat 夹住 kernel」的写法。

#### 4.3.4 代码实践

1. **实践目标**：为一个新算子写基准时知道抄哪里——通过「填空」练习内化模板。
2. **操作步骤**：
   - 通读 `bench/python/ops/bench_flip.py`（比 resize 简单，没有真假平面分支）；
   - 在纸上写出它的三段结构：setup 里读了哪些轴、申报了多少字节、run 里调用的是哪个 `_into` 函数；
   - 对照 `bench/cpp/ops/BenchFlip.cpp` 检查两侧轴集合是否一致；
   - 思考题落地：假设要给 `invert` 算子（逐通道取反）加基准，`add_global_memory_reads/writes` 各应申报多少？（答案：各 `N*H*W*C*dtype_size`，读一次写一次。）
3. **需要观察的现象**：flip 的 Python 基准与 C++ 基准在轴读取顺序、skip 条件、填充方式上是否一一对应。
4. **预期结果**：能画出两侧的逐行对照表，确认「同一份配置、同一个模板」的设计承诺成立。纯阅读实践，可立即完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 run 闭包里用 `resize_into` 而不是 `resize`（allocating 变体）？
**答案**：allocating 变体每次调用要走对象缓存查询、可能还要分配输出（u3-l3）；`_into` 把输出分配移到 setup 段，使计时区间只剩 kernel 本身。基准要测的是算子的 GPU 时间，不是绑定层的分配策略。

**练习 2**：`create_stream_cache` 为什么把预热流和测量流分开缓存？如果混用会发生什么？
**答案**：预热发生在 nvbench 创建测量流之前（`launch is None`），只能用当前流；测量必须用 `launch.get_stream()`。混用（测量期返回当前流）会让 kernel 落在空流、nvbench 的事件打在空闲的测量流上背靠背触发，测出虚假的极短时间，最终以「Python 反而比 C++ 快」的对齐失败暴露（docstring 原文见 [bench/python/python_bench_utils.py:L384-L391](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L384-L391)）。

**练习 3**：`state.skip(...)` 与抛异常有什么区别？
**答案**：`skip` 把该轴组合标记为「不适用」并正常跳过（例如 VarShape 输入遇到 NCHW_FAKE 布局时，[bench/python/ops/bench_resize.py:L65-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resize.py#L65-L67)），结果表里显示 Skipped；抛异常则让整个基准失败。skip 表达「这个组合在语义上就不该测」，异常表达「这个组合应该能测但出错了」。

---

### 4.4 模块四：WarmupPolicy——预热策略的双语言统一实现

#### 4.4.1 概念说明

预热（warmup）是在正式计时前先把被测函数跑若干遍。它消除三类系统性偏差：GPU 从低功耗状态升频需要时间、首次调用的惰性初始化（模块加载、句柄创建）、以及指令/数据缓存冷启动。每个算子配置里的 `warmup_iterations` 字段声明需要的遍数（如 resize 的 basic 档是 200）。

但预热也有代价：一个有几十条配置的算子、每条预热几百次，会让 CI 总时长失控。因此需要一个**不修改配置文件就能临时封顶预热次数**的开关——环境变量 `CVCUDA_BENCH_WARMUP_CAP`。`run_bench.py` 把它做成 `--warmup-cap` 命令行参数（传 0 可完全禁用预热），实现方式是把它写进子进程环境变量（[bench/run_bench.py:L1851-L1860](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L1851-L1860)、[L1323-L1326](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/run_bench.py#L1323-L1326)），C++ 可执行文件与 Python 脚本各自读取。

难点在于**两种语言必须对同一个环境变量做完全相同的解析**，否则「封顶 50」在 C++ 侧生效、Python 侧没生效，对齐检查就建立在不同的预热条件上。仓库的解法是双实现 + 共享测试（`bench/tests/test_warmup_policy.py` 同时测两侧语义）。

#### 4.4.2 核心流程

预热次数的解析逻辑（两语言同构）：

```text
resolve_warmup_iterations(configured):
    if configured <= 0:        # 0 = 配置本身就禁用预热
        return configured
    cap = getenv("CVCUDA_BENCH_WARMUP_CAP")
    if cap 不存在:             # 未设置封顶 → 原样返回
        return configured
    return min(configured, parse(cap))

parse(cap):
    必须是非负十进制整数（空串/非数字/溢出 → 报错）
```

预热执行本身：C++ 侧自建一条 CUDA 流，循环执行后同步销毁（[bench/cpp/CppBenchUtils.hpp:L851-L866](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/CppBenchUtils.hpp#L851-L866)）；Python 侧对 `run_fn(None)` 循环（`None` 表示预热期，闭包会返回当前流），结束后 `torch.cuda.synchronize()`（[bench/python/python_bench_utils.py:L424-L459](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L424-L459)）。

#### 4.4.3 源码精读

**（1）C++ 侧**。`WarmupPolicy.hpp` 定义常量与两个函数：`parse_warmup_cap` 手写逐字符解析（显式拒绝非数字与 `int` 溢出，报错信息都带上环境变量名方便定位），`resolve_warmup_iterations` 做「配置非正或未设环境变量则原样返回，否则取小」的三行核心逻辑（[bench/cpp/WarmupPolicy.hpp:L18-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/WarmupPolicy.hpp#L18-L57)）。注意语义细节：`configuredIterations <= 0` 直接返回，意味着「配置禁用」优先于封顶——设 `warmup_iterations: 0` 的算子不会被 cap 变成 0 以外的值；而无参重载版本负责从环境取值。

**（2）Python 侧**。`_internal/warmup.py` 是同一策略的镜像：同一个环境变量名 `CVCUDA_BENCH_WARMUP_CAP`、同样的「非负十进制」校验（Python 侧额外把上限钳在 C++ `int` 最大值 2147483647，保证两侧可接受的范围一致）、同样的取小逻辑（[bench/_internal/warmup.py:L16-L38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/warmup.py#L16-L38)）。`run_warmup` 在执行前先调用 `resolve_warmup_iterations(iterations)` 应用封顶（[bench/python/python_bench_utils.py:L449](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/python_bench_utils.py#L449)）。

**（3）C++ 的组合入口 `warmup_and_exec`**。所有算子基准统一通过它完成「预热 + 计时」，lambda 只写一份（[bench/cpp/CppBenchUtils.hpp:L907-L912](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/CppBenchUtils.hpp#L907-L912)）：先 `warmup(n, func)`，再 `exec_with_sync`。`exec_with_sync` 值得单独理解——它给 `state.exec` 加 `nvbench::exec_tag::sync` 标签：禁用 nvbench 的死锁检测、改用 CPU 计时而非 GPU 事件、声明 kernel 内部可能自行同步。注释解释了原因：**CV-CUDA 的算子（尤其变长批路径）可能执行内部同步**（回忆 u5-l2：`ImageBatchVarShape::exportData` 会向流调度拷贝并设置事件栅栏），所以全部基准默认用 sync 标签以匹配 Python 侧行为（[bench/cpp/CppBenchUtils.hpp:L868-L891](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/CppBenchUtils.hpp#L868-L891)）。BenchResize.cpp 三条路径全部以 `warmup_and_exec(state, BENCH_RESIZE_WARMUP_ITERATIONS, ...)` 收尾，预热次数宏来自模块二讲的生成头（[bench/cpp/ops/BenchResize.cpp:L111-L112](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchResize.cpp#L111-L112)）。

#### 4.4.4 代码实践

1. **实践目标**：验证预热对测量稳定性的影响，并验证 `--warmup-cap` 在 Python 侧生效。
2. **操作步骤**：
   - 在 `build-rel/bin` 下运行 `python3 bench_resize.py --config-key resize_contract_area_tensor_uchar3_basic`（该配置预热 200 次）记下 GPU 时间与噪声；
   - 加环境变量再跑：`CVCUDA_BENCH_WARMUP_CAP=0 python3 bench_resize.py --config-key resize_contract_area_tensor_uchar3_basic`（禁用预热）；
   - 分别再跑一两次，对比两组的 GPU 时间与噪声百分比；
   - 阅读单驱动用法说明（[bench/README.md:L272-L286](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L272-L286)）：`--list` 列出轴，去掉 `--list` 用 nvbench `--axis` 过滤直接选配置。
3. **需要观察的现象**：无预热组的首次测量时间是否明显偏大、噪声是否更高；有预热组两次运行之间是否更一致。
4. **预期结果**：无预热时冷启动效应导致时间偏高或噪声偏大；预热 200 次后测量趋稳。具体差值依赖 GPU 状态，**待本地验证**。另可通过 `python3 run_bench.py --operator resize --warmup-cap 5` 确认总入口的封顶参数被传递（README L373-L374）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `resolve_warmup_iterations` 对 `configuredIterations <= 0` 直接返回，而不是也取 `min(0, cap)`？
**答案**：`min` 语义下 `configured<=0` 时结果本来就是 `configured`（cap 是非负数），直接返回等价且省一次环境变量解析；更重要的是把「配置显式禁用预热」和「用 cap 限制预热」两个意图分开，避免任何歧义（[bench/cpp/WarmupPolicy.hpp:L45-L52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/WarmupPolicy.hpp#L45-L52)）。

**练习 2**：`exec_tag::sync` 改用 CPU 计时，为什么对可能内部同步的算子反而更可靠？
**答案**：GPU 事件计时假设「事件之间恰好包含全部被测工作」。若算子内部跨流同步（如变长批导出数据时的事件栅栏），测量流上的事件可能无法完整框住实际工作，计时失真甚至死锁检测误报。sync 标签声明这种情况并换用宿主侧计时，以少量精度换取正确性（[bench/cpp/CppBenchUtils.hpp:L868-L877](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/CppBenchUtils.hpp#L868-L877)）。

**练习 3**：Python 侧为什么把 cap 上限钳在 2147483647？
**答案**：该值最终也要传给 C++ 侧（同一个环境变量、同一个进程族），C++ 侧存 `int`；如果 Python 允许更大的数而 C++ 溢出报错，两侧行为就不一致。钳制共享上限是「双语言同一策略」的一部分（钳制检查在 [bench/_internal/warmup.py:L22-L23](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/_internal/warmup.py#L22-L23)，常量定义在 L13）。

---

### 4.5 模块五：compare_to_baseline.py——与提交基线的性能回归对比

#### 4.5.1 概念说明

模块一的门禁（噪声、对齐）只保证「这一次测量本身是好的」；`compare_to_baseline.py` 回答另一个问题：**这次测得的时间相对仓库维护的参考值有没有退化**。它把 `run_bench.py --output bench_output.json` 产出的 JSON 与 `bench/config/operators/*.json` 里内嵌的基线逐行比对。

比对结果分五类，任何一类非空都算失败（`any_fail` 属性，[bench/compare_to_baseline.py:L70-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L70-L88)）：

| 类别 | 含义 |
|---|---|
| regressions | 比基线慢超过 `--regression`（默认 10%） |
| improvements | 比基线快超过 `--improvement`（默认 10%）——**也是失败**，因为可能意味着基线过期 |
| missing_in_current | 基线里有、这次没测到 |
| new_in_current | 这次测到、基线里没有 |
| missing_sku | 用例存在但缺当前 SKU 的数据 |

「变快也失败」是性能回归工具的反直觉设计：基线是提交在仓库里的契约，无故大幅变快往往说明基线没跟着代码更新（stale），下次真回归时阈值就失灵了。

#### 4.5.2 核心流程

```text
main(argv)
  ├─ 加载配置索引（operators/ 全部 JSON）并先自检基线合法性与 SKU 表
  ├─ baseline_updates_from_jsons([--current])   # 解析本次运行的 JSON
  ├─ _resolve_current_sku                       # 从 JSON 解析 SKU；多个 SKU 时必须 --sku 指定
  ├─ compare_updates(updates, index, sku, thresholds, operators)
  │     ├─ 基线侧：遍历 index，取该 SKU 下每个 case 的 cpp/python 时间 → 集合 B
  │     ├─ 当前侧：解析 JSON 中每条 metric → 集合 C
  │     ├─ B − C → missing_in_current
  │     ├─ C − B → 若该 case 有其他 SKU 数据 → missing_sku；否则 → new_in_current
  │     └─ B ∩ C → 逐行 delta = cur/base − 1
  │           delta >  +10% → regressions
  │           delta <  −10% → improvements
  ├─ 打印 Resolved SKU 与五类计数
  ├─ 输出报告：控制台 / --markdown / --junit
  └─ 退出码：any_fail ? 1 : 0   （输入/配置异常 → 2）
```

#### 4.5.3 源码精读

**（1）阈值与比对核心**。`Thresholds` 数据类承载两个 0.10 默认值（[bench/compare_to_baseline.py:L44-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L44-L47)）。比对主体 `compare_updates` 先做三道前置校验：算子选择合法、当前 JSON 的 SKU 必须唯一匹配解析出的 SKU、按当前数据出现的 tier 过滤基线侧（保证 basic 档运行只与 basic 档基线比）（[bench/compare_to_baseline.py:L188-L201](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L188-L201)）。核心分类逻辑对每个交集行计算 `delta = cur_us / base_mean - 1.0`，超过正阈值记回归、超过负阈值记改进（[bench/compare_to_baseline.py:L225-L248](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L225-L248)）。

**（2）基线侧如何取数**。`_baseline_rows_for_sku` 遍历配置索引，对每个用例键检查当前 SKU 是否存在，然后分别以 `gpu_time_us_cpp` 与 `gpu_time_us_python` 两个字段生成两行——**C++ 与 Python 各自成行独立比对**，任何一侧回归都会被抓到（[bench/compare_to_baseline.py:L96-L132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L96-L132)）。

**（3）SKU 解析**。`_resolve_current_sku` 从当前 JSON 收集出现的 SKU：一个则自动用之（报告里显示 `current JSON SKU key` 来源），多个则要求 `--sku` 显式指定，否则报错——防止拿 A100 的运行去比 H100 的基线（[bench/compare_to_baseline.py:L427-L443](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L427-L443)）。README 的示例输出 `Resolved SKU: A100_PCIE_40GB_250W_1095MHz (current JSON SKU key)` 与 `matched=720 regressions=0 ...`、`all-rows |Delta|: median 0.30%, max 5.61%` 展示了健康运行的样貌（[bench/README.md:L221-L236](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L221-L236)）。

**（4）报告输出**。Markdown 报告汇总五类计数、阈值、全体行的 |Delta| 中位数与最大值，再分节列出每类明细（[bench/compare_to_baseline.py:L297-L347](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L297-L347)）；JUnit XML 报告把回归、意外改进、缺失、新增各写成一个 failure 类型的 test case，failure 总数 = 五类之和，可直接接入 CI 的测试面板（[bench/compare_to_baseline.py:L350-L415](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L350-L415)）。`main` 收尾时返回 `1 if result.any_fail else 0`（[bench/compare_to_baseline.py:L576-L594](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L576-L594)）。

**（5）使用约束**。README 强调若做范围对比（`--operator resize`），**生成 JSON 的那次运行也必须带同样的 `--operator resize`**——输入 JSON 必须只含被选中的算子，否则 `compare_updates` 的「未选择的算子出现在 JSON 中」校验会报错（[bench/README.md:L213-L215](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L213-L215)、[bench/compare_to_baseline.py:L182-L186](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L182-L186)）。

#### 4.5.4 代码实践

1. **实践目标**：完成本讲的综合任务——量化「同一台机器上两次运行的性能是否稳定」，并体验基线对比工具的失败路径。
2. **操作步骤**：
   - 第一轮（快速扫描不同规模）：`cd build-rel/bin && python3 run_bench.py --lang python --operator resize --output sweep1.json`，记录各 config_key（不同 batch/形状/dtype）的 `gpu_time_us` 与 `bwutil`，整理成「配置 → 时间 → 推算吞吐（像素数/时间）」表；
   - 第二轮（同配置复测）：同样命令输出 `sweep2.json`；
   - 手工对比：`python3 -c` 读两个 JSON，逐 config_key 计算 `t2/t1 - 1`，列出偏差分布（中位数、最大值）；
   - 基线对比（若你的 GPU 是 A100 PCIe 40GB 或 H100 PCIe）：`python3 bench/compare_to_baseline.py --current build-rel/bin/sweep1.json`，观察五类计数与退出码；若是其他 GPU，预期会得到 SKU 相关的报错，记录报错信息并解释原因。
3. **需要观察的现象**：两次运行的逐行偏差是否都在个位数百分比；`bwutil` 高的配置是否时间也稳定；基线工具对你的 SKU 给出什么反馈。
4. **预期结果**：锁频成功且无后台负载时，同机两次运行的逐行 |Delta| 中位数应在 1% 量级（README 示例为 0.30%）；若出现 10% 以上的行，先排查是否有时钟漂移或共享 GPU 的其他进程。非 A100/H100 机器无法走基线对比，属预期行为。本实践依赖 GPU 环境，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么「比基线快 15%」也被判失败？
**答案**：基线是仓库里维护的性能契约。无故大幅变快通常意味着基线已过期（例如某次优化后忘了更新基线）；过期的基线会让下一次真回归落在阈值内而漏报。所以 improvements 与 regressions 一样需要人工确认并走基线更新流程（[bench/README.md:L216-L219](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L216-L219)）。

**练习 2**：`missing_sku` 与 `new_in_current` 的区别是什么？
**答案**：两者都是「当前测到了、基线里没有」。若该用例键下存在**其他** SKU 的基线数据、只是没有当前 SKU，归 `missing_sku`（用例是已知的，缺这台机器的测量）；若用例键本身不在基线里，归 `new_in_current`（新增了未登记基线的配置）。区分见 [bench/compare_to_baseline.py:L213-L223](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/compare_to_baseline.py#L213-L223)。

**练习 3**：为什么必须用 JSON 输出而不是 CSV 来做基线对比？
**答案**：JSON 是基线导入/导出工具链（`_internal/baselines.py` 的 `baseline_updates_from_jsons` 等）交换的载体，携带 SKU 身份与语言标注等结构化字段；CSV 是面向人读的合并报表。README L69-L74 明确「结果将与基线比对或导入时用 JSON」。

---

## 5. 综合实践

**任务：为一个算子建立「配置—运行—报告」的完整基准档案。**

以 `flip` 或 `gaussian` 为对象（比 resize 简单），完成以下闭环：

1. **配置研读**：从 `bench/config/bench_params.json` 找到该算子的条目；打开对应的 `operators/<op>.json`，选出 3 个不同规模的 basic 档 config_key（覆盖不同 batch 或形状），在表格里登记：形状、dtype、inputKind、warmup_iterations、两张参考卡上的基线时间。
2. **双语言运行**：分别执行 `python3 run_bench.py --operator <op>`（双语言）与 `--lang python`（单语言），对比输出表：C++ 与 Python 时间差是否在 10%/100µs 内？BWUtil 是多少？
3. **预热实验**：用 `--warmup-cap 0` 与默认各跑一次同一 config_key，记录时间与噪声差异，用模块四的原理解释。
4. **稳定性复测与人工回归对比**：同配置跑两轮输出 JSON，仿照 `compare_to_baseline.py` 的 `delta = cur/base - 1` 公式手工对比两轮，报告 |Delta| 中位数与最大值，给出「性能是否稳定」的结论；如果你的 GPU 恰是 SKU 表中的型号，再用真正的 `compare_to_baseline.py` 验证你的手工结论。
5. **交付物**：一页纸报告，包含配置表、双语言对照表、预热对比、两轮稳定性结论，以及至少一条「如果这个算子要优化，应该先看哪个数字」的判断（提示：BWUtil 接近 1 说明访存已饱和，优化方向是减少读写字节，如融合）。

本综合实践把五个模块串起来：模块一的运行与指标解读、模块二的配置体系、模块三的脚本结构（你需要读 bench_<op>.py 确认被测调用）、模块四的预热策略、模块五的回归对比方法。GPU 相关步骤在无卡环境中无法执行，可先完成第 1 步与读码部分，其余标注「待本地验证」。

## 6. 本讲小结

- `bench/` 是「一算子、双镜像、一份配置」的体系：`bench/cpp/ops/BenchXxx.cpp` 与 `bench/python/ops/bench_xxx.py` 同构，共同吃 `bench/config/operators/<op>.json`；三大入口是 `run_bench.py`（运行）、`compare_wheels.py`（对比两个 wheel）、`compare_to_baseline.py`（对比提交基线）。
- `run_bench.py` 在测前尝试锁定 SM 时钟并全程采样时钟/功耗/温度——频率漂移曾是 40–75% 级别的噪声源；输出表的 `GPU Time / GPU Noise / BWUtil / Py overhead / Status` 各有明确语义。
- 质量阈值集中在 `_internal/quality.py`：噪声 <10%、C++/Python 相对差 <10% 且绝对差 <100µs；「Python 明显慢于 C++」通常指向绑定层缺陷而非语言开销。
- Python 基准是三段式模板（setup → run 闭包 → `run_benchmark` 注册），run 段只含一次 `_into` 调用且必须把 kernel 提交到 `launch.get_stream()`——提交错流会测出「Python 比 C++ 快」的假时间。`perf_utils.py` 的 `CvCudaPerf` 是样例管线的 NVTX/NSYS 工具，与 ops 基准是两套体系。
- 预热策略由 `WarmupPolicy.hpp`（C++）与 `_internal/warmup.py`（Python）双实现同一语义：`CVCUDA_BENCH_WARMUP_CAP` 环境变量对配置值取小，`--warmup-cap 0` 可禁用；C++ 侧统一走 `warmup_and_exec`，并以 `exec_tag::sync` 适配可能内部同步的变长批算子。
- `compare_to_baseline.py` 把运行 JSON 与内嵌在算子 JSON 里的 SKU 基线（当前支持 A100/H100 两种身份）逐行比对，五类异常（回归、意外改进、缺失、新增、缺 SKU）任一非空即退出码 1——「变快也失败」用来逼出过期基线。

## 7. 下一步学习建议

1. **u7-l4（NVTX 埋点与性能分析）**：本讲的 ops 基准告诉你「算子总时间」，下一讲教你用 Nsight Systems 把时间拆到管线内部各段；`perf_utils.py` 的 `CvCudaPerf` 正是那一讲的伏笔。
2. **u8-l4（算子工程工具链）**：`tools/optimize_op.py --phase preflight` 会检查基准覆盖是否就绪——本讲的 config_key 覆盖矩阵与 BWUtil 数字是优化活动（`OPTIMIZATION_GUIDELINES.md` 要求「先基准后改码」）的入场券。
3. **继续阅读的源码**：想深入基线维护，读 `bench/_internal/baselines.py`（case 键解析与基线导入/校验）与 `bench/_internal/update_baseline.py`、`validate_baselines.py`；想理解运行器全貌，读 `run_bench.py` 的 `ResultsProcessor` 与 CSV/JSON 写出路径；想看双语言一致性的守护测试，读 `bench/tests/test_warmup_policy.py` 与 `test_compare_to_baseline.py`。
