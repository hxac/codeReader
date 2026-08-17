# u7-l7 测试体系与贡献流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PyPTO 仓库测试版图的分层：`python/tests/ut`（单元测试）、`python/tests/st`（系统测试）、`ut/interpreter`（主机侧 pass_verify）、`ut/kirin`（按 SoC 分组的单算子 codegen 测试）、`examples`（示例校验）与 `models`（大模型用例）。
2. 讲清楚 pytest 基础设施（`pytest.ini`、根 `conftest.py`）如何完成测试收集、SoC 过滤、多卡设备分配与按耗时重排。
3. 精读 kirin UT 公共库 `common.py`：`compare_cos` 真实余弦相似度、`check_nan` NaN 防护，以及两者为什么必须搭配使用。
4. 理解最新的 camodel 运行方式：`kirin/conftest.py` 设置 `simulation.accuracy_level=2` 后，SIM 用例在 CANN 环境下走 AICORE_MODEL（camodel）路径。
5. 掌握表驱动参数化组织法：`common_cmp.py` 公共模块 + 每个 SoC 一个薄壳测试文件。
6. 读懂 `ut/interpreter` 的主机侧 pass_verify 测试链路（conftest fixtures → golden 注册 → 日志断言）。
7. 按照 `CONTRIBUTION.md` 的 Issue 先行流程与 pre-commit/ruff 检查清单，为自定义算子补充一个合格的单元测试并提交。

## 2. 前置知识

- **UT / ST**：UT（Unit Test，单元测试）只编译/仿真单个算子并与 golden（参考答案）比对；ST（System Test，系统测试）在真机上跑完整执行链路，覆盖调度、启动、多卡等系统行为。
- **golden 对比**：用 torch 在 CPU 上算出"标准答案"，再和 PyPTO 算子的输出比对。浮点输出常用**余弦相似度**（只看方向一致，容忍整体幅值误差）配合阈值；布尔/整数输出用**精确相等**。
- **余弦相似度**：把两个输出展平成向量后计算
  \[ \cos(\theta) = \frac{\sum_i a_i b_i}{\sqrt{\sum_i a_i^2}\,\sqrt{\sum_i b_i^2}} \]
  值域 \([-1, 1]\)，越接近 1 表示两组数值方向越一致。测试里通常要求 \( |\cos| \ge 0.9999 \)。
- **NaN 陷阱**：Python 中 `NaN < 0.9999` 的结果是 `False`。如果输出里混入 NaN，余弦相似度会变成 NaN，阈值断言会**静默放行**——这就是 `check_nan` 必须先于 `compare_cos` 调用的原因。
- **SIM 模式与 camodel**：`RunMode.SIM` 是主机侧仿真。装了 CANN 软件层（存在 `ASCEND_HOME_PATH` 环境变量）且仿真精度等级为 2 时，SIM 用例会走 **AICORE_MODEL（camodel）** 仿真，数值行为比纯 CPU 解释执行更接近真机。
- **pass_verify**：框架在编译/解释过程中对每个算子节点做 golden 校验的机制；`ut/interpreter` 的用例只编译 + pass_verify，不真正 launch 到设备。
- **pre-commit / ruff**：pre-commit 是 git 提交钩子框架；ruff 是 Python 静态检查与格式化工具。本仓库提交前必须通过 pre-commit 检查。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `pytest.ini` | 全局 pytest 配置：测试文件匹配规则、默认测试路径、markers、排除目录 |
| `conftest.py`（仓库根） | 全局 pytest 插件：自定义参数、xdist 多卡设备分配、SoC 过滤、按耗时重排 |
| `python/tests/ut/kirin/common.py` | kirin UT 公共库：`compare_cos`、`check_nan`、bin 文件读写 |
| `python/tests/ut/kirin/conftest.py` | kirin UT 全局配置：日志级别 + `simulation.accuracy_level=2`（camodel） |
| `python/tests/ut/kirin/common_add.py` | 单算子 add 的表驱动公共模块（kernel 工厂 + 用例表 + 运行器） |
| `python/tests/ut/kirin/common_cmp.py` | 六个比较算子（eq/ne/lt/le/gt/ge）共用的表驱动公共模块 |
| `python/tests/ut/kirin/kirin9030/single_operation/test_kirin9030_cmp.py` | 每 SoC 一个的薄壳测试文件，参数化消费 `TEST_CASES` |
| `python/tests/ut/interpreter/conftest.py` | interpreter 测试的 fixtures：强制 pass_verify、CPU 化、只编译不 launch |
| `python/tests/ut/interpreter/_verify_check.py` | golden 注册与 pass_verify 结果断言助手 |
| `python/tests/ut/interpreter/test_apply_adam_w_v2.py` | 代表性 pass_verify 用例：AdamW 融合算子 |
| `examples/validate_examples.py` | 示例批量校验器，由 `build_ci.py --example` 调用 |
| `CONTRIBUTION.md` | 贡献流程与代码规范（pre-commit、ruff） |
| `python/pypto/frontend/parser/entry.py` | `_execute_kernel`：camodel 分支的消费者 |

## 4. 核心概念与源码讲解

### 4.1 测试版图与 pytest 基础设施

#### 4.1.1 概念说明

PyPTO 的 Python 侧测试分四大块，职责互不重叠：

| 位置 | 定位 | 依赖硬件 |
|---|---|---|
| `python/tests/ut` | 单元测试（含 `interpreter`、`kirin`、`ir`、`operator`、`simulator`、`pypto_pro` 等子目录） | kirin/interpreter 用例无需真机 |
| `python/tests/st` | 系统测试（真机完整链路：调度、aclgraph、profiling、多卡等） | 需要 NPU |
| `models/` | 大模型算子用例（glm、deepseek、qwen3_next） | 多为真机 |
| `examples/` | 示例脚本，经 `validate_examples.py` 批量校验 | NPU 或 SIM |

`pytest.ini` 与根 `conftest.py` 是这四块共享的"调度中枢"：前者决定"收哪些文件"，后者决定"怎么跑、在哪张卡上跑、按什么顺序跑"。

#### 4.1.2 核心流程

一次 `pytest` 执行在基础设施层的流程：

```text
读取 pytest.ini
  ├── python_files 匹配 test_*.py / glm_*.py / deepseekv32_*.py / qwen3_next_*.py
  ├── testpaths 限定 models、python/tests/ut、python/tests/st
  └── norecursedirs 排除 experimental、deepseek_v4
收集用例（conftest.py 钩子介入）
  ├── pytest_addoption 注册 --device / --cards-per-case / --test_case_info
  ├── pytest_collection_modifyitems：
  │     ① 非 ut 路径的用例按 SoC 版本过滤（950/910 标签）
  │     ② ut/interpreter 用例（Host pass_verify）跳过 SoC 探测
  │     ③ 按 --cards-per-case 过滤多卡用例
  │     ④ 按 duration_estimate 降序重排（长用例先跑，缩短并行总时长）
  └── pytest-xdist 场景：pytest_configure_node 给每个 worker 分配设备
        └── 写入 TILE_FWK_DEVICE_ID / TILE_FWK_DEVICE_ID_LIST 环境变量
执行用例（进程标题随设备更新，coverage 在 forked 子进程手动保存）
```

#### 4.1.3 源码精读

**pytest.ini：收集规则的单一事实来源**

[pytest.ini:L10-L22](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/pytest.ini#L10-L22)

```ini
[pytest]
python_files = test_*.py glm_*.py deepseekv32_*.py qwen3_next_*.py
testpaths = models python/tests/ut python/tests/st
markers =
    soc: Mark test cases for specific SOC versions (e.g., 950, 910)
    world_size: number of NPU cards required (e.g., 1, 2)
norecursedirs = experimental deepseek_v4
```

说明：`models/` 下的用例文件不是 `test_` 前缀而是按模型命名（`glm_*.py` 等），所以匹配规则里专门列出；`soc` 与 `world_size` 两个 marker 是后面 SoC 过滤与多卡过滤的依据。

**根 conftest.py：自定义参数与设备分配**

[conftest.py:L52-L64](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/conftest.py#L52-L64) 向 pytest 注册 `--device`（设备 ID 列表，ST 场景传入）与 `--cards-per-case`（每个用例需要的卡数）。

[conftest.py:L84-L129](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/conftest.py#L84-L129) 是 `pytest_configure_node`：pytest-xdist fork worker 之前，把设备列表按 `cards_per_case` 切组，给每个 worker 写入 `TILE_FWK_DEVICE_ID`（单卡）或 `TILE_FWK_DEVICE_ID_LIST`（多卡）；设备不够的 worker 直接清掉这两个环境变量（该 worker 只跑无卡用例）。

**根 conftest.py：SoC 过滤与耗时重排**

[conftest.py:L242-L261](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/conftest.py#L242-L261) 的 `_is_case_match_soc` 解析用例上的 `soc` marker（未标记默认支持 `"910"`），并把 SoC 版本号 260 映射为标签 `"950"`。

[conftest.py:L264-L312](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/conftest.py#L264-L312) 的 `pytest_collection_modifyitems` 完成三件事，其中最值得注意的是对 `ut/interpreter` 的豁免：

```python
def _is_verify_case(item):
    # Host pass_verify 用例：python/tests/ut/interpreter（原 tests/verify）
    return "ut/interpreter" in str(item.fspath).lower().replace("\\", "/")
...
# ut/interpreter 看护 Host pass_verify / SIM，不依赖 NPU soc 探测
```

也就是说：跑 NPU 用例前会调用 `torch_npu.npu.get_soc_version()` 探测真机型号，但 `ut/interpreter` 的用例纯主机侧执行，不参与探测与过滤。随后用 `_get_test_time_cost` 读取 `duration_estimate` 装饰器（[conftest.py:L19-L40](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/conftest.py#L19-L40)）标注的预估耗时，把长用例排到前面，让并行执行的总时长更短。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到收集规则、过滤与重排的效果。
2. **操作步骤**：
   - 在仓库根执行 `pytest --collect-only -q python/tests/ut/kirin | head -30`，观察 kirin 用例的节点 ID（含参数化 id）。
   - 再执行 `pytest --collect-only -q python/tests/ut/interpreter | head -20`，对比 interpreter 用例的形态。
   - 打开 `-v` 查看 `test_kirin9030_cmp.py` 的参数化展开：`test_cmp[001]`、`test_cmp[002-skip]` 等。
3. **需要观察的现象**：cmp 的 47 条参数中只有 `001` 无 skip 标记，其余带 `skip`；interpreter 用例是普通函数式用例。
4. **预期结果**：`--collect-only` 不执行任何编译，只打印用例节点；参数化 id 与 `common_cmp.py` 中 `pytest.param(..., id="001")` 一一对应。若本地未装 pypto 包，收集阶段可能因 import 失败报错——此时只阅读输出中的报错位置即可。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pytest.ini` 的 `python_files` 里要列出 `glm_*.py`、`deepseekv32_*.py`、`qwen3_next_*.py`？

**答案**：`models/` 目录下的大模型用例文件按模型名命名而非 `test_` 前缀，默认匹配规则收不到它们；显式列出后这些文件也被纳入 `testpaths = models` 的收集范围。

**练习 2**：`duration_estimate` 装饰器为什么把"长用例排前面"？

**答案**：并行（xdist/多卡）执行时总时长约等于最慢 worker 的时长。长用例先开始、短用例填充尾部空闲，能降低"某个 worker 早就跑完、另一个还在跑一条超长用例"的尾部等待，缩短整体墙钟时间。

**练习 3**：`ut/interpreter` 用例为什么可以不做 SoC 过滤？

**答案**：它们只在主机侧做编译 + pass_verify 校验（conftest 会把 `_execute_kernel` 替换为只编译版本），既不上真机也不依赖设备型号，探测 `torch_npu.npu.get_soc_version()` 反而会在无卡环境直接退出。

### 4.2 kirin UT 公共库：compare_cos、check_nan 与 golden 判定

#### 4.2.1 概念说明

`python/tests/ut/kirin/` 是"按 SoC 分组 + 按算子分文件"的单算子 codegen 测试集。所有算子文件（`common_add.py`、`common_cmp.py`、`common_rmsnorm.py`……约 30 个）都从同一个公共库 `common.py` 导入数值判定工具。

提交 `e71ccb398`（"Update kirin python ut to run camodel"）之前，`compare_cos` 把余弦相似度算出来、写进日志，然后**恒返回 1.0**——也就是数值判定形同虚设，用例永远不会因精度差而失败。该提交让它返回真实计算值，同时新增了 `check_nan`。这两个函数是本节精读重点。

#### 4.2.2 核心流程

单算子浮点用例的判定流程（以 add 为例）：

```text
kernel 输出 output（torch.Tensor，CPU）
  ├── ① check_nan(output, name)        # 有 NaN → 立即 AssertionError
  ├── ② golden = input0 + input1       # torch 参考答案
  ├── ③ cos = compare_cos(output, golden)   # 展平 + float64 + 余弦
  └── ④ abs(cos) < 0.9999 → AssertionError  # 方向偏差过大即失败
```

顺序不能颠倒：NaN 会让 `cos` 变 NaN，而 `NaN < 0.9999` 为 `False`，④ 会静默通过——所以 ① 必须先挡住 NaN。

#### 4.2.3 源码精读

**compare_cos：真实余弦相似度**

[python/tests/ut/kirin/common.py:L19-L36](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common.py#L19-L36)

```python
def compare_cos(input_1, input_2):
    input_1 = input_1.reshape(-1).astype(np.float64)
    input_2 = input_2.reshape(-1).astype(np.float64)
    logger.info("max diff: %s", np.max(np.abs(input_1 - input_2)))
    index = np.argmax(np.abs(input_1 - input_2))
    logger.info("max diff index = %s, ...", index, input_1[index], input_2[index])
    logger.info("average diff: %s", np.mean(np.abs(input_1 - input_2)))
    ab = np.sum(input_1 * input_2)
    aa = np.sqrt(np.sum(input_1 * input_1))
    bb = np.sqrt(np.sum(input_2 * input_2))
    if aa == 0 and bb == 0:
        cos = 1.0
    elif aa == 0 or bb == 0:
        cos = 0.0
    else:
        cos = ab / (aa * bb)
    logger.info("cosine similarity: %s", cos)
    return cos
```

要点：

- 先 `reshape(-1).astype(np.float64)` 展平并升精度，避免 fp16 求和溢出/精度损失干扰判定。
- 日志先输出 max diff、最大差异位置和平均 diff——失败时不用重新跑就能定位最差元素。
- 全零向量特判：两个都全零记 1.0（完全一致），只有一个全零记 0.0（方向无意义）。
- `return cos` 是本次提交的关键修改点（旧代码此处写死 `return 1.0`）。

**check_nan：NaN 防护**

[python/tests/ut/kirin/common.py:L39-L47](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common.py#L39-L47)

```python
def check_nan(tensor, name=""):
    """Raise AssertionError if tensor contains any NaN values."""
    if isinstance(tensor, torch.Tensor):
        arr = tensor.detach().cpu().numpy()
    else:
        arr = np.asarray(tensor)
    if np.issubdtype(arr.dtype, np.floating) and np.isnan(arr).any():
        prefix = f"{name}: " if name else ""
        raise AssertionError(f"{prefix}tensor contains NaN values")
```

要点：兼容 torch.Tensor 与 numpy 数组；只对浮点 dtype 检查（布尔输出如 eq/lt 的结果天然无 NaN）；`name` 参数让报错信息直接指明是哪个 kernel。

**调用侧：add 用例的三步判定**

[python/tests/ut/kirin/common_add.py:L587-L593](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common_add.py#L587-L593)

```python
golden = input0 + input1

check_nan(output, name=kernel_name)
cos_value = compare_cos(output.cpu().numpy(), golden.cpu().numpy())
cos_value = abs(cos_value)
if cos_value < 0.9999:
    raise AssertionError(f"{kernel_name}: cos_value {cos_value} < 0.9999")
```

这就是 kirin 浮点用例的标准判定模板：`check_nan` → `compare_cos` → 阈值 0.9999。取 `abs` 是为了容忍"整体取反"这类极端情况按方向一致处理（实践中几乎不会触发，主要是防御性写法）。

#### 4.2.4 代码实践

1. **实践目标**：用最小 numpy 实验验证"NaN 会骗过余弦阈值"以及 `check_nan` 的必要性。
2. **操作步骤**（示例代码，在任意可运行 numpy 的环境执行）：
   ```python
   import numpy as np
   golden = np.array([1.0, 2.0, 3.0, 4.0])
   bad = np.array([1.0, np.nan, 3.0, 4.0])
   ab = np.sum(bad * golden); aa = np.sqrt(np.sum(bad * bad)); bb = np.sqrt(np.sum(golden * golden))
   cos = ab / (aa * bb)
   print(cos, cos < 0.9999)          # nan False —— 阈值断言放行！
   print(np.isnan(bad).any())        # True —— check_nan 能抓住
   ```
3. **需要观察的现象**：`cos` 打印 `nan`，`cos < 0.9999` 打印 `False`（NaN 与任何数比较均为 False）。
4. **预期结果**：确认只靠 `if cos_value < 0.9999` 无法发现 NaN 输出，`check_nan` 是必需的前置防线。此实验纯 numpy，可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：`compare_cos` 为什么在计算前把数据转成 `np.float64`？

**答案**：大张量在 fp16 下做 `\(\sum a_i b_i\)` 这类连加极易累积舍入误差甚至溢出，判定工具自身的数值噪声会淹没被测算子的真实误差；float64 让判定结果稳定可信。

**练习 2**：布尔输出的 cmp 用例（4.3 节）为什么改用 `np.testing.assert_array_equal` 而不用 `compare_cos`？

**答案**：布尔向量的"余弦相似度"只衡量 True/False 分布的方向，0.99 的相似度对应着大量比特错误，语义完全不对；比较算子的输出必须逐元素精确相等才有意义。

**练习 3**：如果某个 fp16 算子输出整体放大了 2 倍（每个元素都 \(\times 2\)），`compare_cos` 会判失败吗？这合理吗？

**答案**：不会——余弦相似度对整体缩放不敏感（分子分母同比例放大），cos 仍为 1.0。这是该判定方式的已知取舍：它抓"方向/形状"错误，不抓"幅值"错误；需要严格幅值校验时应配合 max diff 日志或换用相对误差断言。

### 4.3 camodel 运行模式与表驱动参数化组织

#### 4.3.1 概念说明

本模块讲两件由提交 `e71ccb398` 一并落地的变化：

1. **camodel 运行模式**：`kirin/conftest.py` 在收集阶段设置全局配置 `simulation.accuracy_level=2`。此后该目录下所有声明 `RunMode.SIM` 的 jit kernel，只要环境里装了 CANN（存在 `ASCEND_HOME_PATH`），执行时就会走 **AICORE_MODEL（camodel）** 仿真路径，而不是退回主机侧 CPU 解释执行——数值行为更接近真机，这就是提交标题"run camodel"的含义。
2. **表驱动参数化**：`common_cmp.py` 展示了"一份公共模块服务多个 SoC"的组织法——算子分发表 + 用例参数表 + kernel 工厂 + 运行器全部写在公共模块里，每个 SoC 只留一个几行的薄壳测试文件。

#### 4.3.2 核心流程

**camodel 分支的决策链**（位于前端 jit 执行入口 `_execute_kernel`）：

```text
执行 jit kernel
  ├── 环境变量 CAMODEL_LOG_PATH 存在？
  │     └── 是 → 强制 run_mode = SIM（camodel 调测环境优先）
  ├── run_mode == NPU？
  │     └── 是 → LaunchKernelTorch 真机执行
  └── 否（SIM）：
        ├── accuracy_level == 2 且 ASCEND_HOME_PATH 存在（装了 CANN）
        │     └── 是 → 走 LaunchKernelTorch 的 camodel（AICORE_MODEL）仿真
        └── 否 → DeviceInit + compile + _run_with_cpu（主机 CPU 解释执行）
```

**表驱动组织的分工**：

```text
common_cmp.py（公共模块，SoC 无关）
  ├── CMP_OPS：op 名 → (pypto 算子, torch 算子) 分发表
  ├── make_cmp_kernel()：按 soc_version 现场构造 jit kernel（SIM 模式）
  ├── TEST_CASES：pytest.param 参数表（kernel 名/op/精度/tile/shape/marks）
  ├── run_cmp_test()：造输入 → 跑 kernel → golden 比对
  └── create_test_cmp_module(soc_version)：批量建 kernel 注册表
test_kirin9030_cmp.py（薄壳）
  └── KERNELS = create_test_cmp_module("Kirin9030")
      @pytest.mark.parametrize(..., TEST_CASES) 消费同一张表
test_kirinx90_cmp.py（另一个薄壳）
  └── create_test_cmp_module("KirinX90")，其余完全相同
```

#### 4.3.3 源码精读

**kirin/conftest.py：一行配置启用 camodel**

[python/tests/ut/kirin/conftest.py:L11-L18](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/conftest.py#L11-L18)

```python
import logging

import pypto

logging.getLogger().setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO)

pypto.set_global_config("simulation.accuracy_level", 2)
```

`set_global_config` 的定义在 [python/pypto/config.py:L751-L753](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/config.py#L751-L753)，它把 `{"global.simulation.accuracy_level": 2}` 写进 C++ 侧全局配置域。conftest 在测试模块导入**之前**执行，因此模块级的 `@pypto.frontend.jit(...)` 装饰发生时配置已生效。把日志级别开到 INFO，是为了让 `compare_cos` 里的 max diff / cos 日志直接可见。

**entry.py：camodel 分支的消费者**

[python/pypto/frontend/parser/entry.py:L636-L660](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/frontend/parser/entry.py#L636-L660)

```python
def _execute_kernel(self, torch_tensors, tensor_defs):
    """Run kernel on NPU or CPU (SIM)."""
    cannsim_is_configed: bool = bool(os.environ.get("CAMODEL_LOG_PATH"))
    if cannsim_is_configed:
        self._runtime_options["run_mode"] = RunMode.SIM
    if self._runtime_options.get("run_mode", None) == RunMode.NPU:
        pypto_impl.LaunchKernelTorch(self, _current_stream(), torch_tensors, tensor_defs)
    else:
        cann_is_configed: bool = bool(os.environ.get("ASCEND_HOME_PATH"))
        if pypto.get_global_config("simulation.accuracy_level") == 2 and cann_is_configed:
            with pypto.options("jit_scope"):
                self._set_config_option()
                get_torch_npu()
                pypto_impl.LaunchKernelTorch(self, _current_stream(), torch_tensors, tensor_defs)
        else:
            pto_tensors = self._convert_tensors_with_metadata(torch_tensors, tensor_defs)
            with pypto.options("jit_scope"):
                self._set_config_option()
                pypto_impl.DeviceInit()
                self.compile(pto_tensors)
                self._run_with_cpu(pto_tensors, [])
```

注意 camodel 分支复用了 `LaunchKernelTorch`——它需要 `get_torch_npu()`（借助 torch_npu 建立 device 上下文），但不占用真实 NPU 计算资源，而是把 kernel 交给 AICORE_MODEL 仿真器。`runtime_debug_mode=2` 在 [python/pypto/config.py:L516-L530](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/config.py#L516-L530) 的文档中即标注为 "enable AICORE_MODEL simulation"。三个前提缺一不可：SIM 模式 + 精度等级 2 + 装了 CANN。

**common_cmp.py：算子分发表驱动 kernel 与 golden**

[python/tests/ut/kirin/common_cmp.py:L26-L33](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common_cmp.py#L26-L33)

```python
CMP_OPS = {
    "eq": (pypto.eq, torch.eq),
    "ne": (pypto.ne, torch.ne),
    "lt": (pypto.lt, torch.lt),
    "le": (pypto.le, torch.le),
    "gt": (pypto.gt, torch.gt),
    "ge": (pypto.ge, torch.ge),
}
```

同一张表同时驱动两件事：被测 kernel 体内调用哪个 pypto 算子、golden 参考用哪个 torch 算子——新增一个比较算子的边际成本接近零。

[python/tests/ut/kirin/common_cmp.py:L36-L52](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common_cmp.py#L36-L52) 的 `make_cmp_kernel` 是 kernel 工厂：按传入 `soc_version` 构造 SIM 模式的 jit kernel，函数体只有 `out[:] = pypto_op(a, b)` 一行计算，`kernel.__name__ = name` 让日志与报错能区分 47 个 kernel。

[python/tests/ut/kirin/common_cmp.py:L55-L76](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common_cmp.py#L55-L76) 是 `TEST_CASES` 参数表的开头，每条 `pytest.param` 携带 8 个字段（kernel 名、op 名、torch 精度、pypto 精度、tile、两个 shape、标量值）加 `marks` 与 `id`。本次提交为每条参数插入了 `op_name` 字段，并把 kernel 名从全部 `eq_kernel_*` 改为按算子命名（`ne_kernel_*`、`lt_kernel_*`……）。

[python/tests/ut/kirin/common_cmp.py:L632-L655](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/common_cmp.py#L632-L655) 的 `run_cmp_test`：造随机输入（标量用 `torch.full_like` 展开）、广播求输出 shape、调 kernel、与 `torch_op` 的结果**精确比对**：

```python
kernels[kernel_name](a, b, out)

expect = torch_op(a, b)
out_np = np.array(out.cpu())
expect_np = np.array(expect.cpu())

check_nan(out, name=kernel_name)
np.testing.assert_array_equal(out_np, expect_np)
```

**薄壳测试文件：一个 SoC 只有十几行**

[python/tests/ut/kirin/kirin9030/single_operation/test_kirin9030_cmp.py:L16-L27](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/kirin9030/single_operation/test_kirin9030_cmp.py#L16-L27)

```python
from kirin.common_cmp import TEST_CASES, create_test_cmp_module, run_cmp_test
import pytest

KERNELS, _ = create_test_cmp_module("Kirin9030")


@pytest.mark.parametrize(
    "kernel_name,op_name,torch_dtype,pypto_dtype,tile_shape,shape_a,shape_b,scalar_val",
    TEST_CASES,
)
def test_cmp(kernel_name, op_name, torch_dtype, pypto_dtype, tile_shape, shape_a, shape_b, scalar_val):
    run_cmp_test(KERNELS, kernel_name, op_name, torch_dtype, shape_a, shape_b, scalar_val)
```

`kirinx90/single_operation/test_kirinx90_cmp.py` 结构完全相同，只是传入 `"KirinX90"`。要给两个 SoC 同时加用例，只改公共模块的 `TEST_CASES` 一处。`from kirin.common_cmp import ...` 要求 `python/tests/ut` 位于 `sys.path`：CI 由 `build_ci.py` 的 `--utest_module` 参数把模块名映射为 `python/tests/ut/<子目录>` 路径调度（[build_ci.py:L1968-L1983](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/build_ci.py#L1968-L1983)）；本地手工跑可在 `python/tests/ut` 目录下执行 pytest 或设置 `PYTHONPATH`，**待本地验证**。

#### 4.3.4 代码实践

1. **实践目标**：体验"加一条用例只改一张表"。
2. **操作步骤**：
   - 复制 `TEST_CASES` 中 `id="001"` 的那条参数，把 `id` 改为 `"048"`、kernel 名改为 `eq_kernel_fp32_048`、`op_name` 保持 `"eq"`、精度换成 `torch.float32` / `pypto.DT_FP32`、shape 换成 `(8, 16)` 之类（不要动源文件，先在自己脑中/草稿里推演，或复制到临时目录试验）。
   - 预测：这条用例会构建一个新 kernel 并精确比对 eq 结果。
3. **需要观察的现象**：`pytest ... -k 048 -v` 只运行新增用例；输出中可见 kernel 编译日志（INFO 级）与 PASS。
4. **预期结果**：参数表的 `marks` 为空时用例被执行；`shape_b` 传 `None` 且 `scalar_val` 给值时走标量分支（`torch.full_like` 展开）。本实践涉及真实编译，**待本地验证**（需要已安装 pypto 与 CANN 环境；无 CANN 时 accuracy_level=2 分支不生效，退回 CPU 解释执行路径）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `simulation.accuracy_level=2` 写在 conftest.py 而不是每个测试文件里？

**答案**：conftest 在该目录所有测试模块导入前执行一次，配置对整个 kirin 测试集全局生效；写进每个文件既重复又容易在新增文件时遗漏，导致同一目录下用例跑了不同仿真精度。

**练习 2**：`CAMODEL_LOG_PATH` 存在时把 `run_mode` 强制改成 SIM，这保护了什么场景？

**答案**：camodel 调测环境中即使测试文件声明了 NPU 模式（或默认 NPU），也没有真实设备可 launch；强制 SIM 让用例在该环境下自动降级为仿真执行，避免直接报"NPU 不可用"。

**练习 3**：薄壳文件里 `KERNELS, _ = create_test_cmp_module("Kirin9030")` 在模块导入期就构建了全部 kernel（jit 装饰），这有什么代价与好处？

**答案**：代价是收集阶段/导入期就触发编译配置工作，导入变慢；好处是 47 条参数化用例共享同一批已构建的 kernel 注册表，每条用例只做"查表调用 + 比对"，用例间零重复构建，且 `kernel.__name__` 让日志可按 kernel 名区分。

### 4.4 interpreter 主机侧 pass_verify 测试

#### 4.4.1 概念说明

`python/tests/ut/interpreter/` 是**无需任何硬件**的数值正确性用例集：它把 jit kernel 的执行替换为"编译 + 框架 pass_verify 逐算子 golden 校验"，全程 CPU 张量。代表用例 `test_apply_adam_w_v2.py` 覆盖了一个完整的 AdamW 融合算子（cast/mul/add/div/sqrt/assemble 混合，双层 tile 循环，动态 shape）。

这一层依赖三个配套文件：

- `interpreter/conftest.py`：五个 autouse fixture，把"NPU 执行"改造为"主机 verify"。
- `_verify_check.py`：注册 golden、断言 verify 日志无失败。
- `_ops/apply_adam_w_v2_golden.py`：纯 torch 实现的 golden 参考答案。

#### 4.4.2 核心流程

```text
导入测试模块前
  └── pytest_configure：patch torch.npu.is_available → True
       （jit 装饰器导入期会探测 NPU，无卡环境会炸）
每条用例执行时（autouse fixtures）
  ├── _enable_pass_verify：set_verify_options(enable_pass_verify=True, filter=["all"])
  ├── _force_950_platform：soc("950") 用例强制 DAV_3510 + Ascend950 + EXECUTE_GRAPH 阶段
  ├── _host_verify_no_device_memcpy：monkeypatch setup_verify_data，改喂 CPU 张量
  ├── _compile_only_no_launch：monkeypatch JitCallableWrapper._execute_kernel
  │     └── 只做 _convert_tensors → DeviceInit → compile（不 LaunchKernelTorch）
  └── _cpu_friendly_npu_apis：把 .npu()/synchronize() 打成 no-op
用例本体
  ├── torch 造输入 + golden（_ops/apply_adam_w_v2_golden）
  ├── set_verify_goldens([...])：按位置注册输出 golden（None 表示跳过）
  ├── 调用 jit kernel（实际走上面的 compile+verify 路径）
  └── assert_pass_verify_ok()：检查 verify 日志无 FAILED
```

#### 4.4.3 源码精读

**conftest 的导入期补丁与 verify 开关**

[python/tests/ut/interpreter/conftest.py:L17-L32](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/conftest.py#L17-L32) 在任何测试模块导入前把 `torch.npu.is_available` 补丁为恒 True——因为模块级的 `@pypto.frontend.jit` 装饰器在导入期就会调用 `_set_run_mode()` 探测 NPU，无卡但设置了 `ASCEND_HOME_PATH` 的环境会在导入期直接抛错。

[python/tests/ut/interpreter/conftest.py:L35-L44](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/conftest.py#L35-L44) 打开全量 pass_verify：

```python
@pytest.fixture(autouse=True)
def _enable_pass_verify():
    """Enable full pass_verify for all ut/interpreter kernels."""
    import pypto

    pypto.set_verify_options(
        enable_pass_verify=True,
        pass_verify_pass_filter=["all"],
    )
    yield
```

[python/tests/ut/interpreter/conftest.py:L96-L115](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/conftest.py#L96-L115) 的 `_compile_only_no_launch` 是整个方案的核心 monkeypatch：用"转换张量 → DeviceInit → compile"替换 `_execute_kernel`，kernel 永不 launch，pass_verify 在编译/解释流程内完成数值比对。

**golden 注册与结果断言**

[python/tests/ut/interpreter/_verify_check.py:L24-L26](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/_verify_check.py#L24-L26) 的 `set_verify_goldens` 是 [python/pypto/runtime.py:L178-L200](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/pypto/runtime.py#L178-L200) 中 `set_verify_golden_data` 的薄封装：`None` 占位生成空 `DeviceTensorData`（跳过该参数），torch 张量经 `from_torch` 转 CPU pypto 张量后按指针注册——所以 golden 必须在 CPU 上。

[python/tests/ut/interpreter/_verify_check.py:L29-L48](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/_verify_check.py#L29-L48) 的 `assert_pass_verify_ok` 等价于 C++ 侧 `EXPECT_NO_VERIFY_FAILED`：读取 `LogTopFolder()/verify/interpreter.log`，出现 `result FAILED`、`[VERIFY:FAIL]` 或任意 error/fail 关键词即抛 `AssertionError`。

**用例本体：AdamW 融合算子**

[python/tests/ut/interpreter/test_apply_adam_w_v2.py:L28-L47](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/test_apply_adam_w_v2.py#L28-L47) 声明 SIM 模式、双动态维的 jit kernel，7 个张量参数 + 9 个标量参数 + tile 配置。函数体（[L56-L86](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/test_apply_adam_w_v2.py#L56-L86)）用双层 `pypto.loop` + `pypto.view` 取 tile，完成 AdamW 全部数学（动量、二阶矩、偏差校正、权重衰减），最后 `pypto.assemble` 写回三个输出。

测试函数（[L89-L120](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/interpreter/test_apply_adam_w_v2.py#L89-L120)）的关键三步：

```python
w_g, m_g, v_g = apply_adam_w_v2_golden(weight, grad, m, v, ...)  # torch golden
set_verify_goldens([None, None, None, None, w_g, m_g, v_g])      # 4 输入跳过，3 输出注册
apply_adam_w_v2_kernel_bf16_sim(weight, grad, m, v, weight_out, m_out, v_out, ...)
assert_pass_verify_ok()                                          # 日志无 FAIL
```

golden 列表按 kernel 参数位置对齐：前 4 个是输入张量（无需校验），后 3 个对应 `weight_out/m_out/v_out` 三个输出。

#### 4.4.4 代码实践

1. **实践目标**：走通一个无真机的 pass_verify 用例，理解日志在哪。
2. **操作步骤**：
   - 确认已安装 pypto（无真机也可，本用例全程 CPU）。
   - 执行 `pytest python/tests/ut/interpreter/test_apply_adam_w_v2.py -v --log-cli-level=INFO`。
   - 运行后到 `LogTopFolder()` 对应的输出目录下找 `verify/interpreter.log`（通常在框架日志根目录，具体路径以运行打印为准）。
3. **需要观察的现象**：用例 PASS；interpreter.log 中记录各算子节点的 verify 比对结果，无 `result FAILED` / `[VERIFY:FAIL]`。
4. **预期结果**：`assert_pass_verify_ok()` 不抛错。若把 golden 换成错误数据（例如把 `w_g` 乘 1.1 再注册），重跑应当 FAIL——这就是定位数值问题的最小复现手法。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_enable_pass_verify` 等 fixture 要用 `autouse=True`？

**答案**：该目录下所有用例都依赖"pass_verify 开启 + CPU 化 + 不 launch"这套环境改造；autouse 让用例作者无需逐个声明 fixture，新增用例自动获得一致的执行语义，避免遗漏导致用例意外走上真机路径。

**练习 2**：`set_verify_goldens([None, None, None, None, w_g, m_g, v_g])` 里前四个 `None` 的含义是什么？

**答案**：golden 列表与 kernel 张量参数按位置一一对应；`None` 表示该位置（这里是 4 个输入张量）不注册 golden、跳过校验，只有三个输出张量与 golden 比对。

**练习 3**：`assert_pass_verify_ok` 为什么去读日志文件而不是调用某个返回值的 API？

**答案**：pass_verify 的比对发生在 C++ 框架内部，结果以日志形式落在 `verify/interpreter.log`（`result FAILED` / `[VERIFY:FAIL]` 标记）；Python 侧没有现成的结构化返回接口，于是用日志关键词判定，模拟 C++ 测试里 `EXPECT_NO_VERIFY_FAILED` 的语义。

### 4.5 示例校验器与贡献流程

#### 4.5.1 概念说明

前三节讲"怎么测"，本节讲"怎么跑成建制的验证"与"怎么把代码合进去"：

- `examples/validate_examples.py`：批量执行 `examples/` 下所有示例脚本的校验器，是示例质量的后防线（由 `build_ci.py --example` 调用，见文件头注释）。
- `CONTRIBUTION.md`：贡献流程（Issue 先行、/assign 认领、PR 模板）+ 代码规范（pre-commit 强制检查：clang-format、ruff、codespell 等）。

#### 4.5.2 核心流程

**validate_examples 的执行流**：

```text
main()
  ├── collect_scripts：rglob("*.py") 收集目标目录，跳过自身与 __init__.py
  ├── has_run_mode：文本嗅探脚本是否支持 --run_mode 参数
  ├── run_mode == "sim"？
  │     └── _execute_sim：ThreadPoolExecutor 并行（默认 16 worker），不占设备
  └── run_mode == "npu"？
        └── _execute_npu：设备队列租赁式调度（取卡 → 跑 → 还卡）
  每个脚本：subprocess 执行，注入 TILE_FWK_DEVICE_ID，超时记 TIMEOUT
  汇总：passed/failed 统计，有失败则 exit(1)
```

**贡献流程**：

```text
发现问题/有优化想法
  └── 在 gitcode.com/cann/community 指引下建 Issue（Bug-Report / Requirement / Documentation）
        └── 评论框输入 /assign 或 /assign @yourself 认领
非简单 bug 修复（新特性/新接口/新配置/改流程）→ 必须先 Issue 讨论方案，否则可能被拒
本地开发
  └── pip install pre-commit && pre-commit install
提交 PR
  └── 按 PR 模板填写背景/目的/方案；git commit 自动触发 pre-commit 检查，不通过则拦截
```

#### 4.5.3 源码精读

**validate_examples.py：收集与嗅探**

[examples/validate_examples.py:L34-L55](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/examples/validate_examples.py#L34-L55)：`collect_scripts` 递归收集 `.py`（排除 `validate_examples.py` 自身和 `__init__.py`）；`has_run_mode` 直接读脚本文本找 `'--run_mode'` 字符串，决定子进程命令是否附加该参数。

[examples/validate_examples.py:L58-L71](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/examples/validate_examples.py#L58-L71) 的 `run_script` 用 `subprocess.run` 执行单个脚本，环境变量注入 `TILE_FWK_DEVICE_ID`，超时返回 `rc=-1`。

[examples/validate_examples.py:L83-L106](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/examples/validate_examples.py#L83-L106) 的 `_run_with_device` 是"设备租赁"模式：从一个 `queue.Queue` 取设备、跑完归还——NPU 卡数有限，脚本数可以远多于卡数，靠队列让卡始终满载。

[examples/validate_examples.py:L121-L146](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/examples/validate_examples.py#L121-L146) 的 `main` 支持 `-t`（目标路径）、`-d`（逗号分隔设备列表）、`-m npu|sim`、`-w`（SIM 并行度）与 `--timeout`，结束时有失败即 `sys.exit(1)`，天然可接入 CI 判定。

**CONTRIBUTION.md：流程红线**

[CONTRIBUTION.md:L5-L8](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L5-L8) 两条硬规则：PR 必须按模板填写业务背景/目的/方案；**非简单 bug 修复**（新特性、新接口、新配置、改流程）必须先建 Issue 讨论方案，避免代码被拒合入。[L11-L36](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L11-L36) 列出四类贡献场景（Bug 修复、代码优化、文档纠错、帮他人解 Issue），均通过 `/assign` 认领跟踪。

**CONTRIBUTION.md：pre-commit 检查清单**

[CONTRIBUTION.md:L42-L51](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L42-L51) 要求 `pip install pre-commit && pre-commit install`，此后每次 `git commit` 自动检查、不通过即拦截。[L55-L66](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L55-L66) 的检查项表：行尾空格/末尾换行、YAML/JSON 合法性、大文件/私钥/合并冲突标记、clang-format（v18，C++）、**ruff check（v0.14，E/W/F/I/N 规则族）**、切片冒号空格本地脚本、codespell 拼写。[L69-L76](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L69-L76) 说明 ruff 配置在 `pyproject.toml` 的 `[tool.ruff]`，忽略 E501、行宽上限 120。[L78-L95](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L78-L95) 给出手动运行命令（`pre-commit run --all-files` 等）。

#### 4.5.4 代码实践

1. **实践目标**：跑一次示例校验器 + 一次静态检查，体验提交前的自检闭环。
2. **操作步骤**：
   - SIM 批量校验：`python3 examples/validate_examples.py -t examples/00_hello_world -m sim -w 2`（先拿最小的 hello_world 目录试水，避免全量跑太久）。
   - 静态检查：`pip install pre-commit && pre-commit run ruff-check --all-files`（或 `pre-commit run --all-files` 全量）。
3. **需要观察的现象**：校验器逐脚本打印 `[PASS]/[FAIL]` 与耗时，结束输出 `Result: N passed, M failed, T total`；ruff 输出违规文件与规则码（如 `F401 unused import`）。
4. **预期结果**：hello_world 示例在 SIM 模式通过；ruff 对现有仓库基本无告警（存量代码已过检），对自己新写的文件能抓出导入排序、命名等问题。涉及真实编译执行，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`validate_examples.py` 在 NPU 模式为什么用"设备队列租赁"而不是简单地把脚本平均分给各卡？

**答案**：脚本耗时差异大，平均分配会造成快卡闲置、慢卡积压；队列模式（取卡→跑→还卡）让任意时刻空闲的卡立刻领到下一个脚本，整体吞吐由总工作量而非最慢分组决定。

**练习 2**：你要给 PyPTO 新增一个 `pypto.my_op` 算子，按 CONTRIBUTION.md 应该先做什么？

**答案**：新增接口不属于"简单 bug 修复"，必须先建 `Requirement|需求建议` 类 Issue 说明设计方�案并讨论，随后 `/assign` 认领，避免代码合入被拒。

**练习 3**：ruff 忽略了 E501（行超长），那代码还能随便写长行吗？

**答案**：不能。文档明确行宽上限为 120 字符，E501 只是不做硬性拦截，代码评审仍会要求遵守 120 列约定。

## 5. 综合实践

**任务：为你在 u7-l1 写的可复用 Function（若尚未完成 u7-l1，可用任意融合算子替代，例如 `silu(x) * y`）补充一个符合仓库惯例的 pytest 单元测试，并按 CONTRIBUTION.md 自查。**

要求同时用上本讲的四类知识：表驱动参数化（4.3）、`check_nan + compare_cos` 判定（4.2）、SoC 薄壳组织（4.3）、pre-commit 自查（4.5）。

第一步，仿照 `common_add.py` 写公共模块（**示例代码**，路径建议 `python/tests/ut/kirin/common_silu_mul_demo.py`）：

```python
import pytest
import torch

from kirin.common import check_nan, compare_cos
import pypto


def make_silu_mul_kernel(soc_version, name, dtype, tile_shapes):
    @pypto.frontend.jit(
        codegen_options={"soc_version": soc_version},
        runtime_options={"run_mode": pypto.RunMode.SIM},
    )
    def kernel(
        x: pypto.Tensor([...], dtype),
        y: pypto.Tensor([...], dtype),
        out: pypto.Tensor([...], dtype),
    ):
        pypto.set_vec_tile_shapes(*tile_shapes)
        out[:] = pypto.mul(pypto.silu(x), y)

    kernel.__name__ = name
    return kernel


TEST_CASES = [
    pytest.param("silu_mul_demo_001", torch.float16, pypto.DT_FP16, (64,), (64,), (64,), marks=[], id="001"),
    pytest.param("silu_mul_demo_002", torch.float32, pypto.DT_FP32, (32, 16), (32, 16), (32, 16),
                 marks=[pytest.mark.skip()], id="002"),
]


def run_silu_mul_test(kernels, kernel_name, dtype, shape_x, shape_y):
    x = torch.randn(shape_x, dtype=dtype)
    y = torch.randn(shape_y, dtype=dtype)
    out = torch.empty(torch.broadcast_shapes(x.shape, y.shape), dtype=dtype)

    kernels[kernel_name](x, y, out)

    golden = torch.nn.functional.silu(x) * y

    check_nan(out, name=kernel_name)                      # 先挡 NaN
    cos_value = abs(compare_cos(out.cpu().numpy(), golden.cpu().numpy()))
    if cos_value < 0.9999:                                # 再卡方向相似度
        raise AssertionError(f"{kernel_name}: cos_value {cos_value} < 0.9999")


def create_test_silu_mul_module(soc_version):
    kernels = {
        p.values[0]: make_silu_mul_kernel(soc_version, p.values[0], p.values[2], p.values[3])
        for p in TEST_CASES
    }
    return kernels
```

第二步，写 SoC 薄壳（**示例代码**，`python/tests/ut/kirin/kirin9030/single_operation/test_kirin9030_silu_mul_demo.py`）：

```python
from kirin.common_silu_mul_demo import TEST_CASES, create_test_silu_mul_module, run_silu_mul_test
import pytest

KERNELS = create_test_silu_mul_module("Kirin9030")


@pytest.mark.parametrize("kernel_name,torch_dtype,pypto_dtype,tile_shape,shape_x,shape_y", TEST_CASES)
def test_silu_mul_demo(kernel_name, torch_dtype, pypto_dtype, tile_shape, shape_x, shape_y):
    run_silu_mul_test(KERNELS, kernel_name, torch_dtype, shape_x, shape_y)
```

注意 `@pytest.mark.parametrize` 的参数名列表必须与 `TEST_CASES` 中每条 `pytest.param` 的字段顺序一致、且与测试函数形参一致——这正是 [test_kirin9030_cmp.py:L22-L27](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/python/tests/ut/kirin/kirin9030/single_operation/test_kirin9030_cmp.py#L22-L27) 的对齐方式。

第三步，运行与自查：

1. `pytest python/tests/ut/kirin/kirin9030/single_operation/test_kirin9030_silu_mul_demo.py -v`（可加 `--log-cli-level=INFO` 观察 `compare_cos` 打印的 max diff 与 cos 值）。**待本地验证**。
2. `pre-commit run ruff-check --files <你的两个文件>`，按输出修复导入排序（I）、未用导入（F401）、命名（N802/N806）等问题。
3. 按 [CONTRIBUTION.md:L5-L8](https://github.com/gitcode.com/cann/pypto/blob/e71ccb398205a0170c847b4b8f5d8e5862aedcc5/CONTRIBUTION.md#L5-L8) 自查：这属于新增测试用例，提交 PR 前先在 Issue 里说明目的与方案。

验收标准：用例 001 真实执行且 PASS；故意把 golden 改错（如 `* y` 改 `* y * 1.5`）时能 FAIL；ruff 检查零告警。

## 6. 本讲小结

- 测试版图四层分工：`ut`（无真机的单元/数值测试）、`st`（真机系统测试）、`models`（大模型用例）、`examples`（经 `validate_examples.py` 批量校验）；`pytest.ini` 定收集规则，根 `conftest.py` 管 SoC 过滤、多卡分配与按耗时重排，`ut/interpreter` 用例豁免 SoC 探测。
- kirin UT 的数值判定三件套：`check_nan` 先挡 NaN（NaN 会让 `< 0.9999` 阈值静默放行），`compare_cos` 计算 float64 展平余弦相似度并返回**真实值**（e71ccb398 之前恒返回 1.0、判定形同虚设），浮点用例阈值 0.9999、布尔用例精确相等。
- camodel 运行模式：`kirin/conftest.py` 设置 `simulation.accuracy_level=2` 后，SIM 用例在装有 CANN（`ASCEND_HOME_PATH`）的环境走 AICORE_MODEL（camodel）仿真；`CAMODEL_LOG_PATH` 存在时强制 SIM。
- 表驱动组织法：公共模块（分发表 + 参数表 + kernel 工厂 + 运行器）× 每 SoC 薄壳文件，加用例只改一张表，两个 SoC（Kirin9030/KirinX90）自动同步。
- `ut/interpreter` 用 conftest 的 autouse fixtures 把 jit 执行替换为"编译 + pass_verify"，golden 按参数位置注册（`None` 跳过），结果靠 `verify/interpreter.log` 关键词断言，全程 CPU、无需真机。
- 贡献闭环：非简单 bug 修复必须 Issue 先行；提交被 pre-commit 拦截检查（ruff E/W/F/I/N、行宽 120、clang-format、codespell 等），`pre-commit run --all-files` 可手动全量自检。

## 7. 下一步学习建议

- **动手巩固**：完成第 5 节综合实践后，尝试给你的用例再加一个 `kirinx90` 薄壳版本，体会"一份公共模块、两个 SoC"的维护成本几乎为零。
- **深入 verify 机制**：阅读 `python/pypto/op/verify.py` 与 `docs/zh/tutorials/debug/precision.md`（u7-l3），理解 pass_verify 之外面向用户的精度对比工具，与本讲的测试级 golden 判定对照。
- **回看解释器**：结合 u5-l4（仿真模式与解释器）重读 `framework/src/interface/interpreter/calc*.cpp`，理解 `ut/interpreter` 日志背后的逐指令解释执行细节。
- **CI 视角**：阅读 `build_ci.py` 中 `--utest_module`、`--example`、`--py_cov` 相关参数（约 L315、L1968-L1983），把本讲的 pytest/validate_examples 串进完整 CI 流水线理解。
