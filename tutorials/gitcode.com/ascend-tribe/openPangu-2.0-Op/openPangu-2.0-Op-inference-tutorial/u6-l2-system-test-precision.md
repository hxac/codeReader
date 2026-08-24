# ST 系统测试：精度对比与资源调度

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 ST（System Test，系统测试）与 UT（Unit Test，单元测试）在本仓库中的分工边界，以及 ST 为什么必须依赖真实 NPU 硬件。
2. 读懂 `src/tests/st/conftest.py` 实现的 `@pytest.mark.resources(device=..., npus_per_node=...)` 资源声明机制，并解释 `--device/--nodes/--npus-per-node` 三个命令行参数如何驱动用例筛选（deselect）。
3. 掌握本仓库三种「NPU 算子 vs CPU 参考实现」的精度断言写法：二进制一致（`torch.equal`）、容差比较（`assertRtolEqual` / `torch.allclose`）、数学恒等式验证。
4. 会用 pytest 按设备型号、按关键字筛选并执行某个算子的 ST 用例。
5. 能为一个新的自定义算子独立编写一份符合仓库规范的 ST 测试文件。

## 2. 前置知识

### 2.1 ST 与 UT 的分工

第 1 单元（u1-l3）已经建立过这个认知，这里从「测试工程」的角度再明确一次：

| 维度 | UT（`tests/ut/`） | ST（`tests/st/`） |
| --- | --- | --- |
| 运行环境 | 纯 CPU，无硬件 | 真实 NPU 设备 |
| 验证对象 | host 侧逻辑（Tiling 计算、InferShape 推导） | 端到端数值结果 |
| 驱动框架 | 仓库自研 case executor + faker（见 u6-l1） | pytest + torch_npu |
| 判定方式 | 断言 TilingData 字段 / 输出 shape | NPU 输出与 CPU 参考实现对比 |

ST 回答的问题是：「这颗算子在整个调用链（csrc → aclnn → tiling → kernel）都真实跑通之后，算出来的数对不对」。所以它天然需要 `.npu()` 上设备、需要装好 run 包与 wheel 包（回顾 u1-l4、u3-l4 的双包顺序）。

### 2.2 pytest 的几个基础概念

- **marker（标记）**：用 `@pytest.mark.名字` 给测试函数贴标签，供筛选与钩子处理。未注册的 marker 会产生 `PytestUnknownMarkWarning`。
- **conftest.py**：pytest 的「本地插件文件」。放在某个目录下的 conftest.py 会随该目录被收集而自动加载，其中可以定义 `pytest_addoption`（追加命令行参数）、`pytest_configure`（配置期钩子）、`pytest_collection_modifyitems`（收集完成后修改用例列表）等钩子。
- **deselect**：用例被「取消选中」，既不算通过也不算失败，pytest 会单独统计 `deselected` 数量。

### 2.3 来自前面讲义的认知（直接沿用，不再展开）

- `import omni_custom_ops` 是调用前提——算子挂载靠 import 副作用（u1-l4）。
- 调用形态是 `torch.ops.custom.npu_xxx(...)` 或 `torch_npu.npu_xxx(...)`，两者等价（u1-l4）。
- 输入张量必须 `.npu()` 上设备；多输出算子按签名解包成元组（u3-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/tests/st/conftest.py` | ST 框架本体：注册 `resources` marker、追加 `--device` 等命令行参数、收集期筛选用例 |
| `ascendc/src/tests/st/pytest.ini` | pytest 配置：注册 `resources` marker，消除未知 marker 告警 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py` | 标杆样本 1：二进制一致断言 + 非连续输入构造 + 完整失败诊断 |
| `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py` | 标杆样本 2：最简形态，数学恒等式（A·A⁻¹≈I）断言 |
| `ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py` | 标杆样本 3：多输出 + 容差断言 + aclgraph 图模式对照 |

仓库共有 22 个 ST 测试文件（`Glob: ascendc/src/**/tests/st/*.py` 命中 23 个 `.py`，其中 1 个是框架自带的 conftest.py），全部遵循同一套写法，本讲精读的三个样本分别代表「最严格」「最简」「最全面」三种形态。

## 4. 核心概念与源码讲解

### 4.1 resources marker：给用例贴硬件资源标签

#### 4.1.1 概念说明

一台测试机上可能有多块 NPU、多种型号（910B、910_93、950 等），不同用例的需求不同：有的只要 1 块卡，有的要 2 块卡做双卡图模式，有的只允许跑在特定型号上。如果这些信息只写在注释里，调度系统无法机器读取。

`resources` marker 的思路是：**用例自己声明「我需要什么」，命令行声明「现在有什么」，收集期由 conftest 做匹配，不满足的用例直接 deselect**。这样一套测试可以部署在不同规格的集群上，由调度系统按资源挑用例，而不是靠人肉注释。

marker 支持三个关键字参数：

| 参数 | 含义 | 缺省值 |
| --- | --- | --- |
| `device` | 需要的设备型号，支持 fnmatch 通配符（如 `npu:*`、`npu:910B`），也可传列表 | `"*"` |
| `nodes` | 需要的节点（机器）数 | `1` |
| `npus_per_node` | 每节点需要的 NPU 数 | `1` |

仓库实际的声明分布（对全部 ST 文件 grep 统计）：

| marker 形态 | 用例数 |
| --- | --- |
| `@pytest.mark.resources(device="npu:*", npus_per_node=1)` | 60 |
| `@pytest.mark.resources(device="npu:*")` | 20 |
| `@pytest.mark.resources(device="npu:*", npus_per_node=2)` | 8 |
| `@pytest.mark.resources(device="npu:910B", npus_per_node=1)` | 7 |

绝大多数用例是「任意 NPU 型号 + 单卡」，只有 `lower_triangular_inverse` 等少数算子锁死 `910B`；`npus_per_node=2` 的 8 个用例集中在稀疏注意力图模式场景。

#### 4.1.2 核心流程

```text
pytest 启动
  ├─ pytest_addoption        向 --device / --nodes / --npus-per-node 组追加选项
  ├─ pytest_configure        注册 resources marker 说明（防未知 marker 告警）
  ├─ 收集所有测试函数
  └─ pytest_collection_modifyitems   逐条检查：
        取 item 最近的 resources marker
        ├─ 没有 marker            → deselect（不是报错！）
        ├─ device 不匹配          → deselect
        ├─ --nodes 给定且 != 需求  → deselect
        ├─ --npus-per-node 给定且 != 需求 → deselect
        └─ 其余                   → 保留执行
```

device 匹配是**双向通配**：`fnmatch(cli_device, req)` 或 `fnmatch(req, cli_device)` 任一成立即匹配。也就是说用例声明 `npu:910B`、命令行给 `npu:*` 也能配上；两个方向都没指定（`None`）同样视为通过。

注意一个命名细节：**命令行选项用连字符（`--npus-per-node`），marker 关键字用下划线（`npus_per_node`）**，读代码时不要混淆。

#### 4.1.3 源码精读

**第一步：追加命令行参数。**

[conftest.py:L18-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L18-L38) 向 pytest 的 `resources` 参数组注册三个选项：`--device`（字符串选择器，帮助文案给出 `npu:910B, npu:*, *:910B` 示例）、`--nodes`（整数）、`--npus-per-node`（整数）。整个注册包在 `try/except` 里，注释写明「如果参数已经存在，忽略错误」——防止 conftest 被加载两次时因选项重复注册而崩溃。

**第二步：注册 marker。**

[conftest.py:L41-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L41-L49) 用 `config.addinivalue_line` 把 `resources(**kwargs): hardware resource requirements` 写进 marker 白名单，并顺手忽略一条 `pkg_resources` 弃用告警。这与 `pytest.ini`（见 4.2.3）里的注册互为冗余，双保险。

**第三步：双向通配的 device 匹配函数。**

[conftest.py:L56-L78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L56-L78) 实现语义：CLI 未指定（`None`）或用例未声明（`None`）都直接放行；字符串会被包成单元素列表；对每个需求值做 `fnmatch(cli, d)` 或 `fnmatch(d, cli)` 双向匹配。`npu:*` 因此能配上 `npu:910B`，反过来也成立。

**第四步：收集期筛选主逻辑。**

[conftest.py:L85-L125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L85-L125) 是核心钩子 `pytest_collection_modifyitems`：先从 config 取出三个 CLI 值（`None` 表示未指定即通配），然后遍历所有收集到的 item：

- [conftest.py:L95-L99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L95-L99)——`item.get_closest_marker("resources")` 取**最近**的 marker（类的每个方法各自装饰时取方法上的），没有 marker 的用例一律 deselect。注释写得很直白：「没有 resources marker → 不跑」。**这意味着在这个框架下，忘贴 marker 的用例会被静默跳过而不是执行**，是新人最容易踩的坑。
- [conftest.py:L102-L105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L102-L105)——从 marker 的 kwargs 里取需求值，缺省 `device="*"`、`nodes=1`、`npus_per_node=1`。
- [conftest.py:L112-L119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L112-L119)——nodes 与 npus 的规则是**精确相等**：只有 CLI 显式给出且与需求不同才 deselect；CLI 不给就完全不管。注意这里是 `!=` 而不是 `<`，即命令行声明 `--npus-per-node 8` 时，需求 1 卡的用例也会被筛掉——框架把它理解为「精确资源匹配」而非「最低资源匹配」。
- [conftest.py:L123-L125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L123-L125)——被筛掉的用例通过 `pytest_deselected` 钩子上报（终端会显示 `deselected` 计数），最后 `items[:] = selected` 原地替换待执行列表。

#### 4.1.4 代码实践

实践目标：亲手统计并验证 marker 的筛选行为。

操作步骤：

1. 在仓库根目录执行下面的统计命令，核对上文表格中的数字：

   ```bash
   grep -rhoE '@pytest\.mark\.resources\(device="[^"]*"(, npus_per_node=[0-9]+)?\)' \
     ascendc/src --include='*.py' | sort | uniq -c | sort -rn
   ```

2. 打开 `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py` 第 33 行，确认它的 marker 是 `device="npu:910B", npus_per_node=1`；再打开 scatter 的用例第 89 行，确认是 `device="npu:*", npus_per_node=1`。
3. 做一次纯逻辑推演（不需要硬件）：假设执行 `pytest --device npu:910B`，逐条走 4.1.2 的流程图，判断这两个用例各是被选中还是被 deselect；再假设 `--device npu:950PR`，重复一遍。

需要观察的现象／预期结果：`--device npu:910B` 时两个用例都被选中（`npu:*` 双向通配命中）；`--device npu:950PR` 时 scatter 仍被选中（`npu:*` 匹配任意），而 lower_triangular_inverse 被 deselect。实际 pytest 运行验证需要 torch/torch_npu 环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：一个测试类有 20 个方法，其中 18 个方法贴了 marker，2 个忘了贴。执行 `pytest --device npu:910B` 后 2 个忘贴的用例命运如何？

答案：被 deselect，既不通过也不失败，只在统计里计入 `deselected`。依据是 [conftest.py:L97-L99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L97-L99) 的「没有 resources marker → 不跑」分支。如果想让它们失败，只能靠 code review 或额外的统计脚本。

**练习 2**：为什么 `device_match` 要做双向 fnmatch，而不是只判「CLI 是需求的子集」？

答案：因为两侧都可能是通配符。用例声明 `npu:910B`（精确）、CLI 给 `npu:*`（通配任意可用设备）时，只有 `fnmatch("npu:*", "npu:910B")` 这个方向能命中；反之用例声明 `npu:*`、CLI 给 `npu:910B` 靠另一个方向。单向匹配会漏掉其中一种合法组合。

**练习 3**：命令行 `--nodes 2` 会怎样影响声明了 `npus_per_node=2` 但没写 `nodes` 的用例？

答案：该用例的 `nodes` 需求取缺省值 1（[conftest.py:L104](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/conftest.py#L104)），CLI 显式给了 2，`2 != 1` 成立，用例被 deselect。要让它跑，marker 里必须显式写 `nodes=2`。

### 4.2 pytest 配置与执行方式

#### 4.2.1 概念说明

pytest 的行为由三层控制：命令行参数、conftest.py 钩子、ini 配置文件。本仓库 ST 框架的三层分工是：

- `pytest.ini`：只做一件事——注册 `resources` marker 的说明文字。
- `conftest.py`：既追加命令行选项（`pytest_addoption`），又注册 marker（`pytest_configure`），还做筛选（`pytest_collection_modifyitems`）。
- 命令行：`--device` 等参数 + pytest 原生的 `-k`（按名筛选）、`--collect-only`（只收集不执行）、`-v`（逐条列出）。

#### 4.2.2 核心流程

一次典型的 ST 执行：

```text
cd inference/ascendc
pytest src/tests/st \
       src/ops-transformer/index/ai_infra_scatter_block_update/tests/st \
       --device npu:910B -v
  ├─ 加载 src/tests/st/conftest.py（因为该目录是命令行参数之一）
  ├─ 收集两个参数路径下的测试
  ├─ modifyitems 筛选
  └─ 执行选中的用例（每个用例内部：import → 构造输入 → .npu() → 调算子 → 对比）
```

**为什么要把 `src/tests/st` 也写成参数？** pytest 只会自动加载「参数目录及其祖先链上的 conftest.py」。算子自己的 st 目录（`src/ops-transformer/.../tests/st/`）与框架目录（`src/tests/st/`）不在同一条祖先链上，如果只传算子目录，框架 conftest 不会被加载，`--device` 会被 pytest 当成未知参数直接报错。把框架目录一并传入，conftest 作为本地插件被加载，其钩子（包括 `pytest_addoption` 和收集期筛选）就对**整个会话**的所有用例生效。

需要说明：仓库的 README 只给出了目录结构（`tests # 算子测试（st/ut）`），没有提供 ST 的官方执行脚本或 CI 入口，上述调用方式是根据 conftest 位置与 pytest 机制推断的合理形态，**具体执行入口待本地验证**。

#### 4.2.3 源码精读

[pytest.ini:L9-L11](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/st/pytest.ini#L9-L11) 的 `[pytest]` 段只有两行：声明 `resources: hardware resource requirements`。这与 conftest 里 `pytest_configure` 的 `addinivalue_line` 重复注册同一 marker——pytest 对重复注册是幂等的，两处任一生效都能消除 `PytestUnknownMarkWarning`，属于「belt and braces」式写法。

对比另外两个样本文件的「模块级」写法：[test_ai_infra_moe_init_routing_v3.py:L20-L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L20-L23) 用模块级变量 `pytestmark` 一次性给全文件所有用例贴上 `ignore::DeprecationWarning` 与 `ignore::UserWarning` 两个告警过滤器，等价于给每个测试方法都装饰一遍 `@pytest.mark.filterwarnings`——scatter 测试里每个方法都手写一遍这个装饰器（如 [test_ai_infra_scatter_block_update.py:L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L88)），两种写法效果相同，模块级更省行数。

顺带指出两个「驱动入口」的差异：scatter 的测试文件**没有** `if __name__ == "__main__"` 块（全文件 225 行，`run_tests` 只在第 13 行被 import 而从未调用），完全靠 pytest 收集 `unittest.TestCase` 子类来驱动；而 MoE 文件末尾 [test_ai_infra_moe_init_routing_v3.py:L621-L622](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L621-L622) 保留了 `run_tests()`（`torch_npu.testing.testcase` 提供的 `unittest.main()` 封装），即可以用 `python test_xxx.py` 直跑——但这种跑法**不会**经过 conftest 的资源筛选。

#### 4.2.4 代码实践

实践目标：在不占用 NPU 的前提下，用 `--collect-only` 观察 conftest 的筛选结果。

操作步骤：

1. 确认环境已装 torch、torch_npu 且 `import omni_custom_ops` 可成功（否则收集阶段 import 就会失败）。
2. 执行只收集不运行：

   ```bash
   cd inference/ascendc
   pytest src/tests/st \
          src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st \
          src/ops-transformer/index/ai_infra_scatter_block_update/tests/st \
          --collect-only -q
   ```

3. 再加上 `--device npu:950PR` 重复一次。
4. 对比两次输出的 collected / deselected 数量。
5. 追加 `-k "bf16"` 观察按用例名二次筛选的效果。

需要观察的现象／预期结果：第 2 步应收集到两个文件的全部用例；第 3 步中 `npu:910B` 的用例（lower_triangular_inverse 的 1 个）应从 collected 变为 deselected，scatter 的 18 个用例因 `npu:*` 仍然 collected。本实践需要完整的软件栈，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果直接 `pytest src/ops-transformer/index/.../tests/st/`（不传框架目录），会发生什么？

答案：`src/tests/st/conftest.py` 不在该目录的祖先链上，不会被加载，于是没有任何代码注册 `--device` 选项——若命令行带 `--device` 会报「unrecognized arguments」；不带则不会做任何资源筛选，且 `resources` marker 因两处注册都没生效而产生未知 marker 告警（用例照常执行）。

**练习 2**：`pytest.ini` 里注册了 marker，conftest 的 `pytest_configure` 又注册了一次，为什么不算冲突？

答案：`addinivalue_line` / ini 的 `markers` 都是向同一个 marker 白名单追加说明行，pytest 对重复注册幂等处理，只是避免 `PytestUnknownMarkWarning` 的双保险。

**练习 3**：想只跑 MoE 算子中「pangu_l0」系列用例，用什么命令？

答案：结合 4.2.2 的调用形态，追加 `-k "pangu_l0"`：`pytest src/tests/st src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st --device npu:* -k "pangu_l0"`。用例命名如 [test_ai_infra_moe_init_routing_v3.py:L340](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L340) 的 `test_ai_infra_moe_init_routing_v3_int8_ND_r0_s1_pangu_l0_000000`，`-k` 按子串匹配命中。

### 4.3 NPU 与 CPU 的精度对比：三种断言写法

#### 4.3.1 概念说明

ST 的灵魂是「对拍」：同一组输入，CPU 上用 PyTorch/NumPy 算出参考答案（业内常称 golden / 标杆），NPU 上跑自定义算子，然后比较。怎么比，取决于算子的数值性质，本仓库有三种成熟模式：

| 模式 | 断言方式 | 适用场景 | 样本 |
| --- | --- | --- | --- |
| A. 二进制一致 | `torch.equal`（逐位相等） | 纯搬运/索引类算子，不涉及算术 | scatter_block_update |
| B. 容差比较 | `assertRtolEqual` / `torch.allclose` | 有浮点算术，允许舍入误差 | MoE、稀疏注意力 |
| C. 数学恒等式 | 验证 `A·A⁻¹ ≈ I` 这类性质 | 参考答案本身难算（求逆） | lower_triangular_inverse |

容差比较的数学语义（`torch.allclose` 的判定条件）：

\[ |a - b| \le \text{atol} + \text{rtol} \cdot |b| \]

即每个元素的绝对误差允许「一个平底（atol）加一个与参考值成比例的斜坡（rtol）」。bf16 只有 8 位尾数（相对精度约 \( 2^{-8} \approx 0.4\% \)），所以 bf16 用例的 rtol 通常要放到 1e-2 量级；fp16 有 10 位尾数，可以收紧到 1e-3 量级。

#### 4.3.2 核心流程

一个对拍用例的通用骨架：

```text
1. 固定随机种子（torch.manual_seed / np.random.seed）
2. CPU 构造输入（randn / randint / 手工构造特殊分布）
3. CPU 上算 golden（纯 torch/numpy 参考实现）
4. 输入 .npu() 上设备 → 调 torch.ops.custom.npu_xxx
5. 输出 .cpu() 拉回主机
6. 必要的后处理（裁剪、padding 对齐）
7. 断言（equal / allclose / 恒等式）
8. 失败时打印诊断（max_diff、mismatch 计数等）再 fail
```

#### 4.3.3 源码精读

**模式 A：二进制一致 + 失败诊断（scatter_block_update）。**

CPU 标杆是一个朴素的 Python 循环：[test_ai_infra_scatter_block_update.py:L16-L23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L16-L23) 定义 `golden_scatter_block_update`——克隆输入后按 `output[indices[k,0], indices[k,1], :] = updates[k, :]` 逐行写入。算子只做搬运不做算术，所以标杆与 NPU 结果必须**逐位相同**。

主流程 [test_ai_infra_scatter_block_update.py:L36-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L36-L83) 中值得逐段看的有四处：

- [L39](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L39) `torch.manual_seed(42)` 固定种子，保证失败可复现。
- [L48-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L48-L51) 用 `randperm` 抽取不重复的二维索引再 `stack` 成 `(T, 2)`，保证索引合法且互不冲突。
- [L63-L71](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L63-L71) 输入上设备后调用原地版本算子 `torch.ops.custom.npu_ai_infra_scatter_block_update_`（名字带下划线，对应 u3-l1 讲过的原地 schema）。
- [L74-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L74-L83) 是断言与诊断的范本：`torch.equal` 不过关时，先算 `max_diff` 与不匹配元素计数，再带着完整上下文（shape、T、dtype、是否非连续）调用 `pytest.fail`。**失败信息里带着复现所需的全部参数**，是 ST 可维护性的关键习惯。

**模式 B：容差比较（MoE InitRouting V3）。**

[test_ai_infra_moe_init_routing_v3.py:L326-L333](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L326-L333) 是四输出算子的对比段：`expanded_x`、`expanded_row_idx`、`expert_tokens_count`、`expanded_scale` 逐一 `self.assertRtolEqual(golden.numpy(), npu.numpy())`。`assertRtolEqual` 来自基类 `torch_npu.testing.testcase.TestCase`（该基类还提供 `run_tests` 等工具），默认容差由所装 torch_npu 版本决定；仓库里也有显式给容差的用法，例如稀疏注意力 GQA 的 aclgraph 用例 [test_npu_sparse_flash_attention_gqa_npu_aclgraph.py:L459](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/tests/st/test_npu_sparse_flash_attention_gqa_npu_aclgraph.py#L459)：`self.assertRtolEqual(npu_output.float(), attention.float(), 0.004, 0.004)`——先 `.float()` 升精度再按 fp16 合理的 rtol=atol=0.004 比较。

注意 MoE 对比的**前处理**同样重要：[L308-L323](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L308-L323) 在比较前对 NPU 输出做了裁剪与 padding 对齐（`post_process_npu_output`，见 [L39-L60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L39-L60)）。这是因为 DropPad 模式下 NPU 输出的有效长度与 golden 不严格同形，**对拍前必须先把两边规约到同一形状**，否则比的是padding 而不是数据。

**模式 C：数学恒等式（lower_triangular_inverse）。**

[test_lower_triangular_inverse.py:L16-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L16-L31) 示范了「不直接算逆矩阵」的思路：[L18-L23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L18-L23) 用「单位阵减去严格下三角随机阵」构造出良态的下三角矩阵；[L24](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L24) 调 `torch_npu.npu_lower_triangular_inverse(x.npu())`；[L28-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L28-L31) 计算 \( X \cdot X^{-1} \) 并与单位阵 \( I \) 做 `torch.allclose(I_output, I, atol=0.002)`。求逆的 CPU 参考实现本身也有浮点误差，用恒等式验证绕开了「谁是对的」的争议。整个文件只有 44 行、一个用例（[L33-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L33-L45)），是全仓库最简的 ST 样板。

#### 4.3.4 代码实践

实践目标：为一个假想的 `my_add` 算子选定断言策略并理解 bf16 容差。

操作步骤：

1. 回答选择问题：`my_add(x, y) = x + y`，x、y 均为 bf16，应该用模式 A 还是模式 B？为什么？
2. 手算容差量级：bf16 尾数 8 位，两个绝对值约 1.0 的数相加，最坏相对舍入误差约为多少？rtol=1e-2、atol=1e-2 是否安全？
3. 阅读本节三个样本的断言行，各抄录一行到笔记里，标注模式 A/B/C 与容差参数。

需要观察的现象／预期结果：

1. 应选模式 B：加法在 NPU 与 CPU 上的舍入路径不同（bf16 在 CPU 上可能经 fp32 中间态），逐位相等几乎必然失败；纯搬运类算子（模式 A 的适用面）才敢用 `torch.equal`。
2. \( 2^{-8} \approx 0.0039 \)，两次舍入最坏约 \( 2^{-7} \approx 0.0078 \)，rtol=1e-2 留有余量，是 bf16 对拍的常规起点。运行验证**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 scatter 算子敢用 `torch.equal`，而 MoE 必须用容差断言？

答案：scatter_block_update 只按索引搬数据，输入位是什么输出位就是什么，不存在算术舍入，标杆与 NPU 结果理论上逐位一致；MoE 路径里有量化（`torch.round(x/scale)`）、fp32↔int8 换算等算术，两条实现路径的舍入顺序不同，必须给容差。

**练习 2**：`torch.allclose(a, b, rtol=0.01, atol=0.01)` 中，参考值 b=1e-6、实际值 a=0.002 时断言通过吗？这说明什么？

答案：阈值为 \( 0.01 + 0.01 \times 10^{-6} \approx 0.01 \)，\(|a-b|=0.00199 < 0.01\)，通过。说明 atol 主导小数值区间的绝对误差容忍度——对接近零的参考值，rtol 形同虚设，atol 必须单独设合理值。

**练习 3**：模式 C 里为什么要构造「单位阵减随机阵」而不是直接随机一个下三角矩阵？

答案：直接随机的下三角阵可能奇异或病态（对角元接近 0），求逆结果爆炸、恒等式验证失真。「I 减去严格下三角」保证对角元恒为 1，矩阵恒可逆且条件数可控，是数值测试构造良态矩阵的标准技巧。

### 4.4 用例工程化：基类、种子、图模式对照与计时

#### 4.4.1 概念说明

除了「算子对不对」，一份可长期维护的 ST 还要解决四个工程问题：

1. **基类复用**：`torch_npu.testing.testcase.TestCase` 提供 `assertRtolEqual` 等现成断言（比裸 `assert` 的失败信息友好），`run_tests` 提供 unittest 直跑入口。
2. **可复现性**：所有随机输入必须固定种子，否则偶发失败无法定位。
3. **执行模式覆盖**：同一个算子在 eager 与 aclgraph（NPU 静态图）下行为可能不同，成熟用例会两条路径都拍。
4. **性能可观测**：大算子单用例可能跑几十秒，用 setUp/tearDown 打点计时，输出耗时分布。

#### 4.4.2 核心流程

```text
TestCase 子类
  ├─ setUp:    记 t0
  ├─ test_xxx: _run_test(参数组合)
  │     ├─ 固定 np/torch 双种子
  │     ├─ 构造输入 → NPU eager 执行 或 aclgraph 捕获执行
  │     ├─ CPU golden
  │     ├─ 形状规约后逐输出断言
  └─ tearDown: 打印 [Timing] 耗时
```

#### 4.4.3 源码精读

**基类与导入。** [test_ai_infra_scatter_block_update.py:L9-L13](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L9-L13) 的导入清单就是 ST 文件的标准开头：`pytest`（marker）、`torch`、`torch_npu`（设备后端）、`omni_custom_ops`（算子挂载，缺了它 `torch.ops.custom` 里查无此算子）、`TestCase, run_tests`。测试类 [L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L87) 继承 TestCase 后，每个方法自成用例，用注释分节组织「基础 case / 非对齐 / D=1 / 非连续 / int64 索引 / 边界」六组场景（如 [L116-L136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L116-L136) 的 D 非 32B 对齐组、[L198-L225](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L198-L225) 的边界组：T=1、最小 shape 1×1×1、D 上限 256、T=65536）——**用例分组对应 kernel 的分支路径**（对齐性、D=1 走 TBuf、尾核处理），这是从 u2-l4 的 kernel 知识反推出的测试设计法。

**非连续输入构造。** [test_ai_infra_scatter_block_update.py:L26-L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L26-L33) 的 `make_noncontig_dim0` 用 `torch.as_strided` 把第 0 维 stride 人为垫大 pad 个元素，专门检验 aclnn 层 `CreateView` 零拷贝路径（u2-l2）是否正确保留了 stride。

**双种子与计时。** [test_ai_infra_moe_init_routing_v3.py:L248-L249](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L248-L249) 同时固定 `np.random.seed(seed)` 与 `torch.manual_seed(seed)`——NumPy 造的输入和 torch 造的输入共享同一可复现基线；[L238-L242](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L238-L242) 的 setUp/tearDown 打出 `[Timing] <方法名> elapsed: X.XXXs`。

**eager 与 aclgraph 双模式。** [test_ai_infra_moe_init_routing_v3.py:L63-L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L63-L87) 把算子包成 `torch.nn.Module.forward`，[L90-L107](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L90-L107) 再经 `torch.compile` 与 `torch.npu.graph(..., auto_dispatch_capture=True)` 捕获成 NPUGraph 后 `replay`；用例参数 `use_aclgraph`（[L276-L293](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L276-L293)）控制走图模式还是 eager 直调。这正好覆盖 u3-l3 讲过的「eager 走 csrc、图模式走 converter」两条路径。

#### 4.4.4 代码实践

实践目标：体会「用例分组 ↔ kernel 分支」的映射，并给已有测试补一个新 case。

操作步骤：

1. 通读 scatter 测试的六组用例注释（[L86-L225](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L86-L225)），列出每组检验的 kernel 路径（对照 u2-l4 的 `maxIndicesPerLoad` 分批与尾核逻辑）。
2. 在本地副本（不要改仓库源码）仿照 [L190-L195](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L190-L195) 新增一个 `test_contig_int8_int64_indices` 用例（int8 dtype + int64 索引的组合现缺）。
3. 故意把其中某个用例的期望改错（例如 golden 里 `updates[k]` 换成 `updates[k] + 1`），观察失败输出。

需要观察的现象／预期结果：第 3 步的失败信息应带上 [L79-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L79-L83) 的完整上下文：shape、T、dtype、noncontig、max_diff、mismatch_elements/total。运行需真机，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么导入清单里必须有 `import omni_custom_ops`，即使代码中从未直接引用该名字？

答案：算子注册靠 import 副作用（u1-l4）：`TORCH_LIBRARY_FRAGMENT/TORCH_LIBRARY_IMPL` 与 torch_npu 镜像挂载都在该包的 `__init__.py` 及其导入的 so 中完成。不 import 它，`torch.ops.custom.npu_xxx` 会抛 `OperatorNotFound`。

**练习 2**：MoE 用例为什么把断言目标 `expanded_scale` 的比较单独加了 `numel() > 0` 与 quant_mode 条件（[L330-L333](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/moe/ai_infra_moe_init_routing_v3/tests/st/test_ai_infra_moe_init_routing_v3.py#L330-L333)）？

答案：该输出是「条件性有效」的：非量化且不传 scale 时它根本不存在（golden 里被置为空张量），量化模式 0 下也无意义。对拍框架必须与算子的可选输出语义同步，否则空张量对空张量的比较会制造假失败或假通过。

**练习 3**：`setUp` 里记的计时为什么不打进用例断言？

答案：计时是**可观测性**信息而非正确性判据——机器负载波动会让耗时不稳定，写进断言会造成 flaky 用例。`tearDown` 里 print 出来供人工或日志分析即可。

## 5. 综合实践

综合实践目标：为假想算子 `my_add`（`z = x + y`，输入输出均 bf16）编写一份完整符合仓库规范的 ST 测试文件 `tests/st/test_my_add.py`，把本讲三个最小模块（resources marker、NPU/CPU 对比、pytest 配置）全部用上。

**前置**：已完成 u6-l3 之前的算子开发（有真实算子更好，没有则按下文写成模板，待算子就绪后即可运行）。

第 1 步：确定目录与文件名。按仓库约定放在算子目录下：`ascendc/src/ops-transformer/<族>/my_add/tests/st/test_my_add.py`。

第 2 步：编写测试文件（示例代码，非仓库原有）：

```python
# 示例代码：test_my_add.py
import pytest
import torch
import torch_npu
import omni_custom_ops
from torch_npu.testing.testcase import TestCase, run_tests

# 模块级告警过滤，等价于给每个用例贴 filterwarnings
pytestmark = [pytest.mark.filterwarnings("ignore::DeprecationWarning")]


class TestMyAdd(TestCase):
    def _run_case(self, m, n, dtype, seed=42):
        torch.manual_seed(seed)                    # 固定种子，保证可复现
        x_cpu = torch.randn(m, n, dtype=torch.float32).to(dtype)
        y_cpu = torch.randn(m, n, dtype=torch.float32).to(dtype)

        # CPU 参考实现：升回 fp32 相加，作为 golden
        golden = (x_cpu.float() + y_cpu.float()).to(dtype)

        # NPU 执行（eager 路径）
        z_npu = torch.ops.custom.npu_my_add(x_cpu.npu(), y_cpu.npu())
        z_cpu = z_npu.cpu()

        # bf16 容差对拍：先 .float() 升精度，再 allclose
        if not torch.allclose(z_cpu.float(), golden.float(),
                              rtol=1e-2, atol=1e-2):
            max_diff = (z_cpu.float() - golden.float()).abs().max().item()
            pytest.fail(f"my_add mismatch! shape=({m},{n}), dtype={dtype}, "
                        f"max_diff={max_diff}")

    @pytest.mark.resources(device="npu:*", npus_per_node=1)
    def test_my_add_bf16_base(self):
        """BF16 基础 case"""
        self._run_case(m=128, n=256, dtype=torch.bfloat16)

    @pytest.mark.resources(device="npu:*", npus_per_node=1)
    def test_my_add_bf16_unaligned(self):
        """n=100：最内维 200 字节，非 32B 对齐"""
        self._run_case(m=64, n=100, dtype=torch.bfloat16)

    @pytest.mark.resources(device="npu:910B", npus_per_node=1)
    def test_my_add_bf16_910b_only(self):
        """锁定 910B 的 case：验证 --device 筛选"""
        self._run_case(m=32, n=32, dtype=torch.bfloat16)


if __name__ == "__main__":
    run_tests()
```

第 3 步：执行（推断的调用形态，见 4.2.2）：

```bash
cd inference/ascendc
pytest src/tests/st src/ops-transformer/<族>/my_add/tests/st -v --device npu:910B
```

第 4 步：需要观察的现象与预期结果：

1. 不带 `--device`：3 个用例全部执行（CLI 缺省即通配）。
2. 带 `--device npu:910B`：3 个全部选中（`npu:*` 双向通配）。
3. 带 `--device npu:950PR`：只有 `test_my_add_bf16_910b_only` 被 deselect，终端 `deselected=1`。
4. 把 golden 故意改成减法：失败信息应打印 `shape/dtype/max_diff` 三要素。

本实践依赖真实昇腾环境与已安装的双包，运行结果**待本地验证**。

## 6. 本讲小结

- ST 框架由 `src/tests/st/` 下的 conftest.py + pytest.ini 构成：conftest 注册 `--device/--nodes/--npus-per-node` 三个选项并实现收集期筛选；marker 声明需求、CLI 声明供给、不匹配即 deselect——**忘贴 marker 的用例会被静默跳过**。
- device 匹配是双向 fnmatch 通配（`npu:*` 与 `npu:910B` 互配），nodes/npus 是「CLI 显式给出才比、且要求精确相等」的语义；marker 关键字用下划线、CLI 选项用连字符。
- 对拍三模式按算子数值性质选型：纯搬运用 `torch.equal` 二进制一致（scatter），浮点算术用 `assertRtolEqual`/`torch.allclose` 容差（MoE、GQA，bf16 从 rtol≈atol≈1e-2 起步），参考答案难算时用数学恒等式（A·A⁻¹≈I，atol=0.002）。
- 工程化要点：`import omni_custom_ops` 不可省；双随机种子固定可复现性；失败必须带 shape/dtype/max_diff 诊断；用例分组对齐 kernel 分支；MoE 示范了 eager 与 aclgraph 双模式对拍与 setUp/tearDown 计时。
- 筛选机制生效的前提是把 `src/tests/st` 一并传给 pytest（conftest 只沿参数目录的祖先链加载）；仓库未提供官方 ST 执行脚本，调用入口待本地验证。

## 7. 下一步学习建议

- 下一讲 u6-l3「综合实战：从零新增一个自定义推理算子」会把本讲的 ST 模板纳入九件套开发清单——为你的 `my_add` 真正补齐 def/tiling/kernel/aclnn/csrc 全链路，并让 `test_my_add.py` 跑绿。
- 建议继续阅读的源码：`ai_infra_kv_rms_norm_rope_cache` 与 `ai_infra_fused_infer_attention_sink` 的 ST 文件（全仓库最重的两个测试），观察它们如何为多模式算子组织参数化用例与多种容差档位；以及 `ai_infra_fused_causal_conv1d` 的 ST 如何驱动「缓存状态机」的增量调用序列（呼应 u4-l4）。
- 若想深入 pytest 机制，对照本讲 conftest 阅读 pytest 官方文档中 `pytest_collection_modifyitems` 与 conftest 加载规则的章节，验证 4.2.2 对执行入口的推断。
