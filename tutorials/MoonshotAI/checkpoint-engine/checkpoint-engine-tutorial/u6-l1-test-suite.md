# 测试体系:GPU 端到端测试与 CPU 单元测试

## 1. 本讲目标

学完本讲,你应该能够:

1. 会用 `pytest tests/ -m "not gpu"` 在**纯 CPU** 环境跑通全部单元测试,并说出 `gpu` 这个 marker 在 `pyproject.toml` 里的注册位置、CI(`.github/workflows/cpu-tests.yml`)如何依赖它。
2. 读懂 `tests/test_update.py` 的**双层自举结构**:pytest 函数只是「发射台」,真正的工作由它用 `subprocess` 拉起的 `torchrun` 子进程完成,而 `torchrun` 又回头执行同一文件的 `__main__` 分支。
3. 理解 checker 子进程如何**复用真实的 `update_weights_from_ipc` 协议栈、只替换 `run`/`post_hook` 两个注入点**来顶替 vLLM,从而端到端验证广播正确性、显存回收与错误传播链。
4. 掌握两种 CPU 替身(test double)范式:`MagicMock` + `TestClient`(test_api.py)与 `ParameterServer.__new__` + `SimpleNamespace`(test_p2p_guard.py)。
5. 能模仿本仓库已有的测试范式,为新逻辑补测试——这是本讲综合实践的落脚点。

## 2. 前置知识

本讲的对象不是某个功能模块,而是**验证功能模块的那套代码**。你需要以下概念:

- **pytest marker(标记)**:用 `@pytest.mark.xxx` 给测试打标签,命令行 `-m` 按标签选择/反选。本项目只定义了一个 marker:`gpu`。marker 应在配置里注册(`pyproject.toml` 的 `[tool.pytest.ini_options].markers`),未注册的 marker 在开启 `--strict-markers` 时会报错,注册本身也是一份「本仓库有哪些测试类别」的活文档。
- **pytest 参数化与 fixture**:`@pytest.mark.parametrize` 让一个测试函数展开成多条用例;`@pytest.fixture` 提供可复用的测试前置对象。
- **测试替身(test double)**:被测代码的依赖太重(需要 8 张 GPU、需要 vLLM、需要分布式环境)时,用一个「形状相同、行为可控」的假对象顶替。Python 里常用三种: `unittest.mock.MagicMock`(自动记录调用)、`patch`(临时替换某个模块属性)、`types.SimpleNamespace`(手工拼一个只有几个属性的对象)。
- **`__new__` 跳过 `__init__`**:`ParameterServer.__init__` 要探测设备、建 TCPStore、可能初始化 P2P store——测试某一段逻辑时不想付这笔钱,就用 `ParameterServer.__new__(ParameterServer)` 创建**未初始化**的实例,再手工填上被测路径真正用到的几个属性。
- **torch.multiprocessing 的 spawn 上下文**:`get_context("spawn")` 提供跨平台安全的进程/队列构造器(子进程重新 import 目标模块,而非 fork),test_update.py 用它把 checker 跑在独立进程里。
- **回顾 u3-l4 与 u4-l1 的两处结论**:① PS 的 `_update_per_bucket` 是「预取 H2D → 装填半区并广播 → 等 ACK → 发张量清单」的四拍循环,worker 出错只回传文本、由 `ret_code` 全体约减后统一下发异常;② worker 侧 `update_weights_from_ipc(zmq_ctx, zmq_handle, device_id, *, run, post_hook)` 中 `run`(装载回调)与 `post_hook`(收尾回调)是留给推理引擎的两个注入点——本讲的端到端测试正是掐住这两个注入点完成的。

数学上本讲只有一个小式子:参数化用例数等于各 `parametrize` 笛卡尔积的基数,例如 test_update 的三组参数各展开 1 条,共 \( 3 \times 1 = 3 \) 条用例。

## 3. 本讲源码地图

`tests/` 目录共 12 个测试文件、约 1800 行,是整个仓库里唯一的「消费者视角」文档:

| 文件 | 标记方式 | 运行环境 | 测什么 |
| --- | --- | --- | --- |
| `tests/test_update.py` | 函数级 `@pytest.mark.gpu` ×2 | **≥2 张 GPU** + torchrun | 广播/P2P 更新的端到端正确性、错误传播、混合输入文件 |
| `tests/test_reuse_pin_memory.py` | 函数级 gpu | 1 张 GPU(单 rank) | 共享 pin 内存池的注册/让位/force 释放语义(u2-l5) |
| `tests/test_inplace_unpin.py` | 函数级 gpu | 1 张 CUDA GPU | inplace pin 后的手动 unpin(u2-l4) |
| `tests/test_xpu_ipc.py` | **模块级** `pytestmark = pytest.mark.gpu` + skipif | Intel XPU + 可编译 SYCL 扩展 | SYCL IPC 句柄原语(u4-l4) |
| `tests/test_device_manager.py` | 大部分无标记;3 个 `gpu`+skipif | 纯 CPU / 真 XPU | DeviceManager 映射表与能力开关(u5-l1) |
| `tests/test_api.py` | 无 | 纯 CPU | metas 端点的 JSON 往返与错误码(u4-l5) |
| `tests/test_p2p_guard.py` | 无 | 纯 CPU | `_update_per_bucket` 的两个能力守卫 |
| `tests/test_rdma_parser.py` | 无(1 个用例无网卡时 skip) | 纯 CPU | `NCCL_IB_HCA` 解析与网卡均分(u5-l5) |
| `tests/test_assign_receiver_ranks.py` | 无 | 纯 CPU | P2P bucket 贪心分配(u5-l6) |
| `tests/test_vllm_compat.py` | 无 | 纯 CPU | 新旧 vLLM 构造签名兼容(u5-l3) |
| `tests/test_xpu_parity.py` | 无 | 纯 CPU | XPU 路径的 mock 平替测试(名字带 xpu 但不需要 XPU) |
| `tests/test_ipc_handler.py` | 无 | 纯 CPU | IPC handler 的分发与线格式(u4-l3) |

配套的「非测试」文件:

- [pyproject.toml:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169) —— 注册 `gpu` marker,这是 `-m "not gpu"` 能工作的根。
- [README.md:L163-L177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L163-L177) —— 官方给的两条测试命令。
- [.github/workflows/cpu-tests.yml:L28-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/.github/workflows/cpu-tests.yml#L28-L30) —— CI 在无 GPU 的 ubuntu-latest 上跑 `pytest -v -m "not gpu" tests/`。

依赖关系:所有测试文件只 import `checkpoint_engine` 的公开与内部符号,不互相依赖;唯一的「跨进程依赖」是 test_update.py 对自身 `__main__` 分支的递归调用。

## 4. 核心概念与源码讲解

### 4.1 pytest markers 与两层测试体系

#### 4.1.1 概念说明

checkpoint-engine 的测试天然分成两层:

1. **GPU 端到端层**:验证「权重字节真的从 PS 的锁页内存流进了另一进程的显存,且数值一致」。这层必须有多张真实 GPU、真实的 NCCL 广播、真实的 CUDA IPC,无法 mock,也无法在 CI 里跑。
2. **CPU 单元层**:验证「纯逻辑」——数据模型的 JSON 往返、解析器、分配算法、分发逻辑、HTTP 端点的错误码。这层通过替身把 GPU/vLLM/mooncake 全部隔离,几秒内跑完,由 CI 把守。

连接两层的就是 `gpu` marker:GPU 测试打上标记,CI 与无 GPU 环境用 `-m "not gpu"` 反选掉;本地有卡时跑 `pytest tests/test_update.py` 不加过滤,标记不产生任何影响。

#### 4.1.2 核心流程

```text
pyproject.toml 注册 marker "gpu"
        │
        ├── GPU 层(打标记)
        │     ├── test_update.py            函数级 @pytest.mark.gpu
        │     ├── test_reuse_pin_memory.py  函数级
        │     ├── test_inplace_unpin.py     函数级
        │     ├── test_xpu_ipc.py           模块级 pytestmark(整文件一次标记)
        │     └── test_device_manager.py    后 3 个用例:gpu + skipif(双保险)
        │
        └── CPU 层(无标记,-m "not gpu" 可跑)
              └── test_api / test_p2p_guard / test_rdma_parser /
                  test_assign_receiver_ranks / test_vllm_compat /
                  test_xpu_parity / test_ipc_handler / test_device_manager 前 14 个用例
```

注意两种「跳过」的区别:`-m "not gpu"` 是**选择**(deselect),用例根本不执行、报告里算 deselected;`pytest.mark.skipif` 是**收集后跳过**(skipped),仍会出现在报告里。test_xpu_ipc 与 test_device_manager 的真 XPU 用例两者叠加:CPU CI 反选掉,带卡环境里若扩展编译失败还能优雅 skip。

#### 4.1.3 源码精读

marker 的注册位置——一行 description 说明了反选写法:

```toml
[tool.pytest.ini_options]
markers = [
    "gpu: marks tests as GPU test (deselect with '-m \"not gpu\"')",
]
```

见 [pyproject.toml:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169),这段声明让 `gpu` 成为合法 marker,并把「CPU-only 用 `-m "not gpu"`」写进了配置本身。

模块级标记的写法——整个文件一次打标,不必逐个函数加装饰器:

```python
pytestmark = pytest.mark.gpu
```

见 [tests/test_xpu_ipc.py:L14](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_ipc.py#L14)。同文件开头 [tests/test_xpu_ipc.py:L1-L6](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_ipc.py#L1-L6) 的 docstring 直接解释了「为什么标 gpu:让 CPU-only CI 跳过」。

「gpu + skipif」双保险的写法:

```python
@pytest.mark.gpu
@pytest.mark.skipif(not _HAS_XPU, reason="requires an Intel XPU device")
def test_real_xpu_device_manager():
```

见 [tests/test_device_manager.py:L137-L139](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L137-L139)。`gpu` 管「有没有卡」,`skipif` 管「这张卡是不是 Intel XPU、SYCL 扩展能不能编译」——两级门控各司其职。

CI 侧的对应物——无 GPU 的 ubuntu-latest 上安装 `.[p2p]` 后反选 gpu:

```yaml
- name: Do CPU tests with pytest
  run: |
    pytest -v -m "not gpu" tests/
```

见 [.github/workflows/cpu-tests.yml:L28-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/.github/workflows/cpu-tests.yml#L28-L30);依赖安装在同文件 [L23-L27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/.github/workflows/cpu-tests.yml#L23-L27)(注意装的是带 `[p2p]` extra 的完整包,所以 mooncake 相关的纯逻辑测试也能收集)。

README 的官方口径——GPU 端到端测试**只能**经 pytest 进:

> `pytest tests/test_update.py` … `test_update.py` are only designed to run with `pytest`. Please don't run it directly with `torchrun`. … Only test_update.py requires GPUs, other tests can be run on CPUs. Only to run CPU tests, use: `pytest tests/ -m "not gpu"`

见 [README.md:L163-L177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L163-L177)。为什么禁止直接 torchrun?4.2 节揭晓:该文件的 `__main__` 分支会主动检查 `PYTEST_CURRENT_TEST` 并拒绝。

#### 4.1.4 代码实践

1. **实践目标**:在纯 CPU 环境确认两层测试的划分是否如上表所述。
2. **操作步骤**:
   ```bash
   pip install -e ".[p2p]" pytest    # 或 pip install .[p2p] pytest
   pytest tests/ -m "not gpu" --collect-only -q
   pytest tests/ --collect-only -q
   ```
3. **需要观察的现象**:第二次(不过滤)比第一次多出的用例应该全部来自 test_update.py、test_reuse_pin_memory.py、test_inplace_unpin.py、test_xpu_ipc.py 和 test_device_manager.py 的 `test_real_xpu_*` 三个;`--collect-only -q` 的输出末尾会给出用例总数。
4. **预期结果**:第一条命令的收集清单里**不含** `test_update.py` 的任何用例;总数与本地是否装有 ibverbs 网卡、mooncake 有关(`test_detect_ibv_list` 等用例可能显示为待收集/skip 条件在运行期才判定)。具体收集条数待本地验证。
5. 本环境无法执行 pytest,以上为基于源码的推演,运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:如果不在 `pyproject.toml` 里注册 `gpu` marker,`pytest tests/` 会发生什么?

**答案**:默认配置下 pytest 只会发一条 `PytestUnknownMarkWarning` 警告,测试照常运行;但一旦 CI 或用户开启 `--strict-markers`,未知 marker 会直接报错。注册的价值在于把「本仓库存在一类 GPU 测试,用 `-m "not gpu"` 排除」固化成配置内的文档([pyproject.toml:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169))。

**练习 2**:`pytest tests/test_update.py -m "not gpu"` 的结果是什么?

**答案**:test_update.py 里仅有的两个测试函数都标了 `gpu`([tests/test_update.py:L239](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L239)、[L293](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L293)),反选后没有用例入选,pytest 会报「no tests ran」并给出 deselected 计数,退出码非 0。

**练习 3**:test_device_manager.py 里 3 个真 XPU 用例为什么 `gpu` 和 `skipif` 两个都要加?只加 `skipif` 行不行?

**答案**:只加 `skipif` 时,在带 CUDA 卡但不带 XPU 的机器上这些用例会以 skipped 出现在报告里(噪音);更重要的是 CPU CI 用 `-m "not gpu"` 统一反选,不依赖每个硬件用例自己写对 skip 条件。`gpu` 负责「这一类需要硬件」的粗分类,`skipif` 负责「具体是哪种硬件」的细分类,二者叠加([tests/test_device_manager.py:L137-L139](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L137-L139))。

### 4.2 test_update.py 的自举结构:pytest 只是发射台

#### 4.2.1 概念说明

分布式代码的测试困境:`ParameterServer` 一构造就要读 `RANK`/`WORLD_SIZE` 环境变量、连 TCPStore,而 pytest 进程是一个普通的单进程——两者天生不兼容。

test_update.py 的解法是**自举(bootstrap)**:同一个文件扮演三个角色——

1. **pytest 模块**:提供 `test_update`/`test_update_with_files` 两个测试函数,负责构造 `torchrun` 命令并用 `subprocess.run` 拉起它;
2. **torchrun 的目标脚本**:torchrun 以多进程方式执行 `python test_update.py <test_name> <rank_list>`,落到该文件的 `__main__` 分支;
3. **测试编排器**:`__main__` 分支再按 test_name 分发到 `run()`/`run_with_files()` 编排函数。

一级火箭(pytest)只负责点火与验收返回码,二级火箭(torchrun 子进程)里才是真正的多进程协作。这个设计还带来一个好处:pytest 进程自己**不需要**初始化任何分布式状态。

另一个容易忽略的细节:该文件在**模块 import 期**就要调用 `DeviceManager()` 和 `get_world_size()`(参数化列表在收集期求值),而无 GPU 机器上 `DeviceManager()` 会抛 TypeError。文件开头用 try/except 兜底成一个 `device_count=lambda: 0` 的替身,保证「import 不炸、收集能过」,把失败推迟到用例运行时的 `assert world_size >= 2`——宁可响亮失败,也不静默跳过。

#### 4.2.2 核心流程

```text
pytest 收集期
  └── import test_update.py
        ├── DeviceManager() 成功 → device_manager = 真实单例
        └── TypeError(无任何设备) → SimpleNamespace(device_count=lambda: 0)
  └── parametrize 在收集期求值三组 rank_list

pytest 运行期(每条参数化用例)
  └── test_update(test_name, rank_list)
        ├── assert device_count() >= 2          ← 无 GPU 机器在此响亮失败
        ├── cmd = [torchrun, --nproc_per_node N, --master_addr localhost,
        │         --master_port 25400, __file__, test_name, json.dumps(rank_list)]
        └── subprocess.run(cmd) → 断言 returncode == 0

torchrun 子进程(N 个 rank,每个都执行同一文件)
  └── __main__ 分支
        ├── 检查 PYTEST_CURRENT_TEST 必须存在(防直接 torchrun)
        ├── 解析 argv: test_name + rank_list(json)
        └── 分发: run(checker_proc, ...) / run(..., need_error=True) / run_with_files(...)
```

三组参数化各自验证的场景(以 8 卡为例,见 [tests/test_update.py:L240-L261](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L240-L261)):

| 组 | rank_list | 覆盖路径 |
| --- | --- | --- |
| 1 | `[[0..3], [4..7], [], [0..7]]` | 前两轮 P2P 子集更新 → 一轮全量广播(`[]`)→ 一轮全员 P2P |
| 2 | `[[]]` + `test_with_remote_error` | 广播中途 worker 注入异常,验证错误传播 |
| 3 | 随机采样出 \( \text{num\_ranks}=0..N \) 共 \( N+1 \) 个子集 | P2P 接收端集合的穷举边界(空集=广播、全集、任意子集) |

#### 4.2.3 源码精读

import 期的设备兜底——保证文件在任何机器上都能被 pytest 收集:

```python
try:
    device_manager = DeviceManager()
except TypeError:
    device_manager = SimpleNamespace(device_module=SimpleNamespace(device_count=lambda: 0))
```

见 [tests/test_update.py:L20-L23](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L20-L23)。`DeviceManager` 在探测不到 npu/xpu/cuda 时抛 TypeError(u5-l1),这里换成永远返回 0 的替身,使 `get_world_size() == 0`、参数化列表退化为空集,但用例条目本身仍能生成。

pytest 侧的「发射台」——只做三件事:断言卡数、拼命令、验收退出码:

```python
cmd = [
    "torchrun", "--nproc_per_node", str(world_size),
    "--master_addr", master_addr, "--master_port", str(master_port),
    __file__, test_name,
    json.dumps(rank_list) if rank_list is not None else "[]",
]
result = subprocess.run(cmd, capture_output=False, text=True,
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        shell=False, check=False)
assert result.returncode == 0
```

见 [tests/test_update.py:L268-L290](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L268-L290)。三个值得注意的选择:`__file__` 让 torchrun 执行的就是这个测试文件本身;`rank_list` 经 `json.dumps` 序列化成第二个 argv 参数跨进程传递;`cwd` 切到仓库根目录,使子进程里的相对路径(如 `/tmp` 临时目录的清理)有稳定基准。`capture_output=False` 意味着子进程日志直接透传到终端,方便排障。

`__main__` 分支——防呆检查 + 三路分发:

```python
if __name__ == "__main__":
    run_with_pytest = "PYTEST_CURRENT_TEST" in os.environ
    if not run_with_pytest:
        print("ERROR: This script is designed to run only through pytest!")
        sys.exit(1)
    assert len(sys.argv) > 2
    test_type = sys.argv[1]
    rank_list = json.loads(sys.argv[2])
    if test_type == "test_no_error":
        run(checker_proc, rank_list, need_error=False)
    elif test_type == "test_with_remote_error":
        run(checker_proc_with_error, rank_list, need_error=True,
            expected_exception=RuntimeError,
            exception_msg="Failed to update weights due to remote errors")
    elif test_type == "test_with_files":
        run_with_files(checker_proc)
```

见 [tests/test_update.py:L324-L348](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L324-L348)。`PYTEST_CURRENT_TEST` 是 pytest 在运行测试期间注入的环境变量,检查它回答的问题是「本文件是否正被 pytest 的调用链执行」:直接 `torchrun test_update.py` 时环境里没有这个变量,直接拒绝退出;而经 pytest → subprocess → torchrun 这条链时,子进程**继承**了 pytest 进程的环境(其中含该变量),检查通过、正常分发。这正是 README「Please don't run it directly with torchrun」的代码化落实。

#### 4.2.4 代码实践

1. **实践目标**:不跑任何 GPU 代码,纯靠阅读推演出 torchrun 的完整命令行。
2. **操作步骤**:
   - 读 [tests/test_update.py:L262-L279](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L262-L279),假设本机 8 卡,手写第 1 组第 3 条用例(`test_no_error`, `rank_list=[[0,1,2,3],[4,5,6,7],[],[0,1,2,3,4,5,6,7]]`)会产生的命令行;
   - 再手写参数化第 2 组对应的命令行(应为 `... test_update.py test_with_remote_error [[]]`);
   - 最后执行 `pytest "tests/test_update.py::test_update" --collect-only -q` 确认收集到的 3 条用例 ID。
3. **需要观察的现象**:命令行里 `--nproc_per_node` 等于设备数、`--master_port` 固定 25400、位置参数是 `__file__` 的绝对路径;collect-only 输出 3 条 `test_update[test_no_error-rank_list0]` 之类的用例 ID。
4. **预期结果**:第 2 组命令行为 `torchrun --nproc_per_node 8 --master_addr localhost --master_port 25400 <绝对路径>/test_update.py test_with_remote_error [[]]`(json 序列化的空列表就是字符串 `[]`)。collect-only 条数与 ID 格式**待本地验证**。
5. 本环境未执行,以上推演自源码。

#### 4.2.5 小练习与答案

**练习 1**:为什么 pytest 函数里写 `assert world_size >= 2`,而不是 `pytest.skip(world_size < 2)`?

**答案**:这是一条**需要**硬件的正确性测试,不是可选功能。skip 会让「机器明明有卡但 torch 只看见 1 张」这类环境故障被静默吞掉;assert 则响亮失败,与 test_device_manager.py 中「An unsupported backend must fail loudly rather than silently skipping」([tests/test_device_manager.py:L68-L75](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L68-L75))的哲学一致。CPU 环境的正确打开方式是用 `-m "not gpu"` 反选,而不是指望它自己 skip。

**练习 2**:三组参数化里,`list(range(get_world_size()))`(全员 P2P)与 `[]`(广播)都覆盖了「所有 rank 都拿到权重」,它们走的代码路径有何区别?

**答案**:**回顾 u3-l4/u5-l6**:`ranks=[]` 走全量广播,owner 即 receiver、H2D 从本地锁页内存拷贝;`ranks=[0..N-1]` 走 P2P 路径,即使接收集合等于全体,数据也经 `_assign_receiver_ranks` 分配、由 mooncake RDMA 单边读拉取。所以这两条用例分别压测两条数据面。

**练习 3**:`master_port` 固定写死 25400 有什么风险?

**答案**:并行跑两个 test_update 用例会撞端口(TCPStore 用 MASTER_PORT+1,torchrun 会合用 MASTER_PORT 本身)。当前实现里两条 pytest 用例是串行执行的,且每轮 update 后 `time.sleep(3)` 等进程组销毁([tests/test_update.py:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L166-L169)),所以写死尚可;若用 `pytest -n auto` 并行就会出问题。

### 4.3 checker 子进程:用真协议栈 + 假业务逻辑顶替 vLLM

#### 4.3.1 概念说明

端到端测试的第二个难题:PS 的对端本该是 vLLM worker,但测试不想(也不能)拉起 vLLM。

回看 u4-l1 的结论:worker 侧真正与推理引擎耦合的只有两个注入点——`update_weights_from_ipc(..., run=装载回调, post_hook=收尾回调)`。test_update.py 的策略由此而来:**保留真实的 `update_weights_from_ipc`(完整的 REP 状态机、真实的 ZMQ、真实的 IPC attach、真实的零拷贝切张量),只把 `run` 换成「逐张量比对」的 checker、`post_hook` 换成 `synchronize`**。vLLM、模型、推理统统缺席,但 ZMQ 协议、CUDA IPC、广播链路、错误传播链全部是真枪实弹。

配套还有一个最小替身:生产环境中 `req_func` 的职责是「把 socket 地址清单交给推理引擎」,测试里直接用 `queue.put`——一个跨进程队列,把地址清单从 PS 进程递到 checker 子进程。控制面的「HTTP 通知」被替换成「队列投递」,语义完全等价。

#### 4.3.2 核心流程

`run()` 的完整编排(每个 torchrun rank 各执行一份):

```text
run(checker_func, rank_list, need_error, ...)
├── rank = int(os.getenv("RANK"))                ← torchrun 注入
├── ctx = get_context("spawn"); queue = ctx.Queue()
├── ps = ParameterServer(auto_pg=True)           ← 进程组按轮建毁(u1-l2)
├── named_tensors = dict(gen_test_tensors(rank)) ← 随机生成 500~5000 个张量
├── proc = ctx.Process(target=checker_func, args=(rank, uuid, named_tensors, queue)); proc.start()
├── ps.register_checkpoint("test", named_tensors=...)
├── ps.gather_metas("test")
├── for ranks in rank_list:
│       ps.update("test", queue.put, ranks=ranks)   ← queue.put 就是 req_func!
│       time.sleep(3)                               ← 等进程组销毁
│       (need_error 时:本轮应抛 RuntimeError,断言消息)
├── ps.unregister_checkpoint("test")
├── queue.put(None)                              ← 毒丸,终止 checker 循环
└── proc.join(); assert proc.exitcode == 0
```

checker 子进程(以 `checker_proc` 为例)的主循环:

```text
set_device(rank); tensors.to(device); zmq_ctx = zmq.Context(); 记录初始显存
while True:
    socket_paths = queue.get()          ← 阻塞等 PS 的 req_func 投递
    if socket_paths is None: break      ← 毒丸退出
    names_to_check = {每个名字: False}
    update_weights_from_ipc(            ← 真协议栈!
        zmq_ctx, dict(socket_paths)[本设备UUID],
        device_id=rank,
        run=lambda weights: check(names_to_check, weights),   ← 假业务逻辑
        post_hook=lambda: synchronize())
    synchronize(); empty_cache(); 记录显存 → memory_history
    assert all(names_to_check.values()) ← 每个张量都被核对过
```

错误路径 `checker_proc_with_error` 则把 `run` 换成:睡随机 0.1~0.5 秒后,仅 rank 0 抛 `RuntimeError("Intentional Error for testing.")`。于是错误传播链的两端各自被断言:

- **worker 端(checker 进程)**:异常经 PS 的 `ret_code` 约减后以 `RuntimeError` 实例下发,checker 在 `trigger_error` 外捕获并断言消息为 `"Some workers failed to update weights"`([tests/test_update.py:L82-L85](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L82-L85));
- **PS 端(主进程)**:`pytest.raises(RuntimeError)` 断言消息含 `"Failed to update weights due to remote errors"`([tests/test_update.py:L337-L344](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L337-L344))。

这两个字符串正对应 ps.py 里错误传播的「一收一发」:[checkpoint_engine/ps.py:L902-L903](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L902-L903)——向 worker 发 `RuntimeError("Some workers failed to update weights")`,同时本端 raise `RuntimeError("Failed to update weights due to remote errors")`。**一次测试同时锁死了协议两端的消息契约。**

`run_with_files` 是第三条编排,把输入从「纯内存张量」换成**三路混合**,专门压测 u2-l2/u2-l3/u2-l4 讲过的三条加载路径:

| 来源 | 位置 | 触发的路径 |
| --- | --- | --- |
| 约 1/3 张量存成 safetensors | `/dev/shm/checkpoint_engine_tests/`(tmpfs) | inplace pin(mmap + cudaHostRegister,加载后源文件被删) |
| 约 1/3 张量存成 safetensors | `/tmp/checkpoint_engine_tests/`(磁盘) | normal pin 的 `_load_checkpoint`(safe_open 读文件) |
| 其余张量 | 内存 `named_tensors` | normal pin 的直接注入 |

每个 rank 只写自己的两个文件、注册 `[自己的磁盘文件, 自己的 shm 文件]` 加内存张量,rank 间靠参数名前缀 `rank{i}.` 天然不冲突。一个值得注意的细节:三段切片下标(`[:n//2]`、`[n//3:2n//3]`、`[n//2:]`)在数学上**有重叠区间**,同名张量会同时出现在文件与内存两个来源里,最终由 `_normal_pin_memory` 的合并顺序决定([checkpoint_engine/pin_memory.py:L283-L285](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L283-L285):先 `_load_checkpoint(files)`、再 `parameters.update(named_tensors)`——内存来源覆盖文件来源)。checker 校验的对象是**全集** `named_tensors`,所以无论走哪条来源,数值都必须对。

#### 4.3.3 源码精读

PS 侧把 `queue.put` 当 `req_func` 传给 update——控制面的最小替身:

```python
ps.register_checkpoint(checkpoint_name, named_tensors=named_tensors)
ps.gather_metas(checkpoint_name)
for ranks in rank_list:
    ps.update(checkpoint_name, queue.put, ranks=ranks)
    time.sleep(3)
```

见 [tests/test_update.py:L163-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L163-L169)。`update` 的签名是 `update(checkpoint_name, req_func, *, timeout, ranks=None)`([checkpoint_engine/ps.py:L569-L575](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L569-L575)),`req_func` 收到的是 `list[tuple[str, str]]`(设备 UUID → ZMQ 地址);生产环境里它是 vLLM 的 `/collective_rpc` 调用,测试里它是 `queue.put`, checker 那头 `queue.get()` 拿到同一份数据。

checker 子进程的核心——真协议栈 + 比对回调:

```python
def check(names_to_check, weights):
    for name, weight in weights:
        if name not in named_tensors:
            continue
        assert (weight == named_tensors[name]).all(), f"Tensor {name} does not match!"
        names_to_check[name] = True

def check_weights(names_to_check, socket_paths):
    socket_paths = dict(socket_paths)
    update_weights_from_ipc(
        _zmq_ctx, socket_paths[device_uuid], device_id=rank,
        run=lambda weights: check(names_to_check, weights),
        post_hook=lambda: device_manager.device_module.synchronize(),
    )
```

见 [tests/test_update.py:L98-L126](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L98-L126)。三个要点:`dict(socket_paths)[device_uuid]` 模拟「每个 worker 只 connect 本设备对应的地址」(u4-l2 讲过 UUID 是跨进程配对的钥匙);`if name not in named_tensors: continue` 是因为广播会送达**全组所有参数**(其他 rank 的 `rank{j}.` 前缀参数),本 checker 只核对属于自己的那份;`names_to_check` 字典保证「每个应有张量都被核对过至少一次」,比单比对一个张量更强。比对本身 `(weight == t).all()` 会触发真实的设备端张量比较。

显存回收的旁路校验——每轮 update 后记录显存占用:

```python
device_manager.device_module.synchronize()
device_manager.device_module.empty_cache()
mem_info = device_manager.device_module.mem_get_info()
memory_history.append(mem_info[1] - mem_info[0])
```

见 [tests/test_update.py:L114-L132](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L114-L132)。多轮 update 后若 IPC 缓冲、广播 buffer 泄漏,`memory_history` 会单调上涨——这些数字最后打印为 `[rankN] Memory change: ...`,给人工检查留了观测点(不做硬断言,属于「观测型断言」)。

错误注入的两级放大——随机延迟让错误落在流水线不同拍:

```python
def error_run(weights):
    time.sleep(random.uniform(0.1, 0.5))
    if rank == 0:
        raise RuntimeError("Intentional Error for testing.")
...
except RuntimeError as e:
    assert str(e) == "Some workers failed to update weights"
```

见 [tests/test_update.py:L72-L85](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L72-L85)。随机 sleep 使 rank 0 的失败可能发生在任意桶的装载阶段,其他 rank 可能已正常收完若干桶——这正是错误传播最难覆盖的「部分成功」状态;所有 checker 无论是否出错,最终都应收到同一个 `RuntimeError`(u3-l4 的「全集群同生共死」)。

混合输入的构造——三路来源 + rank 专属文件名:

```python
files.append(disk_files[rank])        # /tmp/checkpoint_engine_tests/rank{i}_checkpoint.safetensors
safetensors.torch.save_file(tensors_in_dev_shm, dev_shm_files[rank])
files.append(dev_shm_files[rank])     # /dev/shm/... 触发 inplace pin
...
ps.register_checkpoint(checkpoint_name, named_tensors=tensors_in_memory, files=files)
```

见 [tests/test_update.py:L191-L225](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L191-L225),收尾在 [L228-L236](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L228-L236)(rank 0 负责清理两个临时目录)。注意 `/dev/shm` 的文件在注册成功后会被 inplace pin **删除**(u2-l4:tmpfs 上 unlink 后数据页由映射持有),所以每轮测试都要重新 `save_file`。

#### 4.3.4 代码实践

1. **实践目标**:通过阅读理解「错误传播链的两端断言」,并设计一个可观察的扰动实验。
2. **操作步骤**(需要 GPU,**待本地验证**):
   - 运行 `pytest tests/test_update.py -k remote_error`,预期通过;
   - 把 [tests/test_update.py:L72-L76](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L72-L76) 中 `time.sleep(random.uniform(0.1, 0.5))` 临时改成 `time.sleep(0)`(只改测试文件,不动源码),再跑若干次;
   - 观察通过率是否稳定。
3. **需要观察的现象**:去掉随机延迟后,rank 0 的错误几乎总落在第一个桶的装载阶段;无论错误发生在哪一拍,所有 rank 的 checker 都应捕获到同一个 `"Some workers failed to update weights"`,PS 主进程抛 `"Failed to update weights due to remote errors"`。
4. **预期结果**:两种情况下测试均应通过;随机延迟的价值在于覆盖「部分桶已成功」的时序,而不是改变最终结果。若去掉延迟后出现 flaky,说明错误传播在极早期失败时有竞态——那就是真 bug。
5. 本环境无 GPU,结论**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:为什么 checker 用 `queue.get()` 阻塞循环 + `None` 毒丸,而不是让 PS 直接调用 checker 函数?

**答案**:PS 与 checker 必须是**两个操作系统进程**:PS 持有广播的发送端与 ZMQ REQ 端,checker 模拟的 worker 必须持有 REP 端和独立 CUDA 上下文(IPC attach 的消费端),同进程内 REP/REQ 会自锁。`queue.put`/`queue.get` 正是横跨两进程的 `req_func` 通道;`None` 是经典的毒丸(pill)终止约定([tests/test_update.py:L78-L81](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L78-L81))。

**练习 2**:check 回调里 `if name not in named_tensors: continue` 能否删掉?为什么?

**答案**:不能。广播送达的是**全组**的参数清单——包括其他 rank 生成的 `rank{j}.layer...` 张量;而本 checker 的 `named_tensors` 只含本 rank 的 `rank{i}.` 前缀张量。删掉后会在名字不匹配时直接 KeyError,把「过滤别人的张量」误报成失败([tests/test_update.py:L98-L103](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L98-L103))。

**练习 3**:`gen_test_tensors` 故意混合了 bfloat16/float16/float8_e4m3fn/float32 四种 dtype(见 [tests/test_update.py:L30-L49](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L30-L49)),这对零拷贝切张量路径有什么意义?

**答案**:**回顾 u4-l1**:`_extract_weights` 用「字节切片 → view(dtype) → view(shape)」三步还原张量,dtype 的字节宽度和 `aligned_size`(256 对齐)直接决定切分下标。混合 dtype 让同一桶内不同参数的 itemsize/对齐量各异,能压测对齐计算与 FP8 等窄类型的正确性——这也是 FP8 支持的回归基础(u6-l4 的伏笔)。

### 4.4 test_api.py:MagicMock + TestClient 的接口测试范式

#### 4.4.1 概念说明

test_api.py 展示的是**接口层**的 CPU 测试范式,文件开头一句话说明边界:"CPU-only tests for the metas endpoints in api.py"([tests/test_api.py:L1](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L1))。

它测的对象是 `_init_api(ps)` 产出的 FastAPI 应用(u4-l5),而 PS 本尊需要 torch 分布式环境。突破口在于:API 层只依赖 PS 的**方法签名与返回值**(`get_metas`/`load_metas`),不关心实现——于是用 `MagicMock` 顶替 PS,用 fastapi 的 `TestClient` 在同进程内发真实 HTTP 请求,再用 pydantic 的 `TypeAdapter` 做「响应 JSON → 模型」的严格反序列化校验。

这个范式有三个可迁移的要点:

1. **fixture 双件套**:`fake_metas` 造数据,`ps_mock` 造被测依赖,`ps_mock.get_metas.return_value = fake_metas` 一行完成「打桩」;
2. **往返(round-trip)断言**:GET 出来的 JSON 必须能被 POST 原样接受,锁死序列化/反序列化的对称性;
3. **「未被调用」也是断言**:校验失败(422)时必须保证 `ps_mock.load_metas.assert_not_called()`——业务方法连执行的机会都不该有。

#### 4.4.2 核心流程

七个用例把 `/v1/metas` 两个端点的响应矩阵铺满:

```text
                         ┌─ 200 ─ test_get_metas_returns_json(往返等于 fake_metas)
GET  /v1/metas ──────────┤
                         └─ 500 ─ test_get_metas_propagates_ps_error(PS 抛错 → 文本透传)

                         ┌─ 200 ─ test_load_metas_decodes_and_calls_ps(反序列化后调用 load_metas)
                         ├─ 422 ─ test_load_metas_rejects_bad_json(非法 JSON,PS 不被调用)
POST /v1/metas ──────────┼─ 422 ─ test_load_metas_rejects_schema_mismatch(合法 JSON 但结构不符)
                         ├─ 500 ─ test_load_metas_propagates_ps_error(PS 抛错 → 文本透传)
                         └─ 200 ─ test_round_trip_get_then_load(GET 的响应体原样 POST 回去)
```

#### 4.4.3 源码精读

打桩用的假数据——手工构造两层嵌套的 metas 结构:

```python
_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])

def _make_meta(rdma_device: str, ip: str) -> MemoryBufferMetaList:
    return MemoryBufferMetaList(
        p2p_store_addr=f"{ip}:12345", rdma_device=rdma_device,
        memory_buffer_metas_list=[MemoryBufferMetas(
            metas=[ParameterMeta(name="w", dtype=torch.float16,
                                 shape=torch.Size([2, 3]), aligned_size=12)],
            ptr=0x12345678, size=1024)])
```

见 [tests/test_api.py:L18-L39](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L18-L39)。这里再次踩到 u2-l1 的核心问题:`ParameterMeta.dtype` 是 `torch.float16`、`shape` 是 `torch.Size`——pydantic 不原生支持,全靠 data_types.py 的自定义校验器;`_METAS_ADAPTER` 在测试侧扮演「另一个消费者」,独立验证这套序列化确实可往返。

fixture 双件套:

```python
@pytest.fixture
def fake_metas() -> dict[int, MemoryBufferMetaList]:
    return {0: _make_meta("mlx5_0", "192.168.1.1"), 1: _make_meta("mlx5_1", "192.168.1.1")}

@pytest.fixture
def ps_mock(fake_metas) -> MagicMock:
    ps = MagicMock()
    ps.get_metas.return_value = fake_metas
    return ps
```

见 [tests/test_api.py:L42-L53](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L42-L53)。`MagicMock` 的任何属性访问都返回新的 mock,因此 `ps.register_checkpoint`、`ps.update` 等未打桩的方法也都「存在且可调用」——恰好匹配 `_init_api` 闭包只按名字取方法的使用方式(u4-l5 讲过 API 层不用依赖注入、纯闭包捕获)。

happy path——响应体必须与输入模型严格相等:

```python
client = TestClient(_init_api(ps_mock))
resp = client.get("/v1/metas")
assert resp.status_code == 200
assert resp.headers["content-type"] == "application/json"
assert _METAS_ADAPTER.validate_json(resp.content) == fake_metas
ps_mock.get_metas.assert_called_once_with()
```

见 [tests/test_api.py:L57-L65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L57-L65)。`validate_json` 是**严格模式往返**:响应字节 → pydantic 模型,任何一个 dtype/shape 序列化失真都会在这里现形;`assert_called_once_with()` 连「无参调用」这个细节都锁死。

422 路径——校验失败必须挡在业务方法之前:

```python
resp = client.post("/v1/metas", content=b"not a valid json",
                   headers={"content-type": "application/json"})
assert resp.status_code == 422
ps_mock.load_metas.assert_not_called()
```

见 [tests/test_api.py:L89-L97](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L89-L97)(结构不符的变体在 [L100-L109](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L100-L109))。这正是 u4-l5 讲过的机制:pydantic 先校验、校验失败端点函数体不执行——`assert_not_called()` 把这个「不执行」变成了可回归的契约,防止有人把校验挪进端点函数导致脏数据进入 PS。

往返测试——GET 的输出字节直接当 POST 的输入:

```python
get_resp = client.get("/v1/metas")
load_resp = client.post("/v1/metas", content=get_resp.content, ...)
assert load_resp.status_code == 200
ps_mock.load_metas.assert_called_once_with(fake_metas)
```

见 [tests/test_api.py:L126-L139](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L126-L139)。这是 join 复用模式(u6-l3)的最小保障:实例 A 导出的 metas 必须能被实例 B 无损导入,否则跨进程权重复用无从谈起。

#### 4.4.4 代码实践

1. **实践目标**:模仿现有范式,为 test_api.py 尚未覆盖的端点补一个用例。
2. **操作步骤**:
   - 打开 [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py),对照 u4-l5 的端点表找出 test_api.py 没测的端点(`GET /v1/healthz`、`POST /v1/checkpoints/{name}/files`、`DELETE /v1/checkpoints/{name}` 都没有用例);
   - 新建一个本地分支(不改主干),在 tests/test_api.py 末尾仿照现有风格补:
     ```python
     # 示例代码(测试草稿,非项目原有代码)
     def test_healthz_returns_200(ps_mock: MagicMock) -> None:
         client = TestClient(_init_api(ps_mock))
         resp = client.get("/v1/healthz")
         assert resp.status_code == 200

     def test_register_files_calls_ps(ps_mock: MagicMock) -> None:
         client = TestClient(_init_api(ps_mock))
         resp = client.post(
             "/v1/checkpoints/ckpt/files",
             content=json.dumps({"files": ["/tmp/a.safetensors"]}).encode(),
             headers={"content-type": "application/json"},
         )
         assert resp.status_code == 200
         ps_mock.register_checkpoint.assert_called_once_with("ckpt", files=["/tmp/a.safetensors"])
     ```
   - 运行 `pytest tests/test_api.py -m "not gpu" -v`。
3. **需要观察的现象**:两个新用例是否通过;`register_checkpoint` 的调用参数是否与你的断言一致(若 api.py 实际传参方式不同,`assert_called_once_with` 会给出清晰的 diff)。
4. **预期结果**:healthz 用例应直接通过;register 用例可能因实际传参(如关键字参数)与断言不完全一致而失败——按 mock 报告的真实调用修正断言即可。本环境无法执行,**待本地验证**。
5. 注意:以上代码是**示例代码**,请勿直接提交;真实参数以 api.py 当前实现为准。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `ps_mock.get_metas.side_effect = RuntimeError(...)` 能让端点返回 500,而不是让测试直接崩掉?

**答案**:`side_effect` 设为异常类/实例时,mock 被调用会**抛出**该异常;FastAPI 端点里包了 `wrap_exception`(u4-l5)——它捕获业务异常转成 500 响应。于是「PS 抛错」被安全地转化成可断言的 HTTP 状态码与文本([tests/test_api.py:L68-L73](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L68-L73))。

**练习 2**:`_METAS_ADAPTER.validate_json(resp.content) == fake_metas` 与直接 `resp.json() == fake_metas.json()` 相比,强在哪里?

**答案**:前者把响应字节按**同一套 pydantic 模型**严格反序列化——若响应里 dtype 写成了 `"float16"` 而校验器只认 `torch.float16` 的某种特定编码,或 shape 丢了维度,validate 阶段就会抛错;后者只是字节层面的字典比较,无法发现「JSON 合法但模型不接受」的失配。这正是 join 模式真实消费者的行为([tests/test_api.py:L57-L65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L57-L65))。

**练习 3**:test_api.py 全程没有标记 gpu,但它 import 了 torch。在没有 GPU 的 CI 上为什么能跑?

**答案**:它只用 torch 的 dtype/Size 对象做数据构造(`torch.float16`、`torch.Size([2,3])` 是纯 Python 层对象),从不调用 `torch.cuda` 或初始化分布式;mock 掉 PS 后,被测的只剩 FastAPI + pydantic 的纯控制面逻辑。

### 4.5 test_p2p_guard.py:__new__ 替身与「守卫先于副作用」断言

#### 4.5.1 概念说明

test_p2p_guard.py 测的是 `_update_per_bucket` 开头的两个**能力守卫**(u5-l1 讲过的「入口硬失败」哲学):设备不支持 IPC、或不支持 P2P 时,必须在函数一开始就抛出清晰的 RuntimeError,而不是带着不透明的错误深入数据面。

难题依旧是:构造一个真 `ParameterServer` 要探测设备、建 TCPStore。这里的替身手法比 MagicMock 更「手工」:`ParameterServer.__new__(ParameterServer)` 绕过 `__init__` 拿到裸实例,再只填被测路径会用到的三个属性。设备管理器则用 `SimpleNamespace` 手拼——`device_type` 是字符串、两个能力开关是 lambda,恰好是守卫代码读取的全部接口。

这类测试的价值在于一个精细的顺序断言:**守卫必须先于任何副作用**。`ipc_handler.export.assert_not_called()` 断言的不是「抛了错」,而是「抛错之前没有导出过任何 IPC 句柄」——如果守卫写在 export 之后,失败路径会泄漏已导出的设备内存映射。

#### 4.5.2 核心流程

```text
_ps_with_device(device_type, supports_ipc, supports_p2p)
  ├── ps = ParameterServer.__new__(ParameterServer)   ← 跳过 __init__
  ├── ps._rank = 0                                     ← 守卫异常消息里用到
  ├── ps.device_manager = SimpleNamespace(             ← 手拼设备管理器
  │       device_type=device_type,
  │       supports_device_ipc=lambda: supports_ipc,
  │       supports_device_p2p=lambda: supports_p2p)
  └── ps._current_global_parameter_metas = {0: object()}  ← 让开头的非空断言通过

test_p2p_update_rejected_on_xpu / test_ipc_unavailable_rejected
  ├── patch dist.is_initialized → True                 ← 绕过「分布式未初始化」前置
  ├── pytest.raises(RuntimeError, match=...)
  └── 调 ps._update_per_bucket(..., ipc_handler=MagicMock(), ...)
        └── 断言:抛了指定消息的 RuntimeError,且 ipc_handler.export 从未被调用
```

两个用例与 ps.py 守卫的一一对应:

| 用例 | 替身配置 | 触发 ps.py 的守卫 | match 的消息片段 |
| --- | --- | --- | --- |
| `test_p2p_update_rejected_on_xpu` | xpu, ipc=True, p2p=False, `ranks=[0]` | [ps.py:L782-L788](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L782-L788) | `P2P weight update .* is not supported` |
| `test_ipc_unavailable_rejected` | xpu, ipc=False, p2p=False, `ranks=None` | [ps.py:L766-L772](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L766-L772) | `cross-process device-tensor IPC` |

#### 4.5.3 源码精读

替身构造——三个属性刚好是从函数入口到守卫之间的全部依赖:

```python
def _ps_with_device(device_type: str, *, supports_ipc: bool, supports_p2p: bool) -> ParameterServer:
    ps = ParameterServer.__new__(ParameterServer)
    ps._rank = 0
    ps.device_manager = SimpleNamespace(
        device_type=device_type,
        supports_device_ipc=lambda: supports_ipc,
        supports_device_p2p=lambda: supports_p2p,
    )
    # Non-empty metas so the leading assert passes; content is irrelevant (guard fires first).
    ps._current_global_parameter_metas = {0: object()}
    return ps
```

见 [tests/test_p2p_guard.py:L17-L27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_p2p_guard.py#L17-L27)。对照 [checkpoint_engine/ps.py:L759-L760](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L759-L760) 的函数开头:`assert len(self._current_global_parameter_metas) != 0` 与 `assert dist.is_initialized()`——前者用 `{0: object()}` 喂饱(注释明说内容无关),后者用 patch 绕过。**替身属性清单就是被测函数真实依赖的清单**,这是 `__new__` 手法的副产品:它逼你读出守卫之前到底碰了 `self` 的哪些字段。

P2P 守卫用例——match 用正则吃掉变量部分:

```python
def test_p2p_update_rejected_on_xpu():
    ps = _ps_with_device("xpu", supports_ipc=True, supports_p2p=False)
    ipc_handler = MagicMock()
    with (
        patch.object(dist, "is_initialized", return_value=True),
        pytest.raises(RuntimeError, match=r"P2P weight update .* is not supported"),
    ):
        ps._update_per_bucket(
            "ckpt", req_func=lambda _paths: None,
            ipc_handler=ipc_handler, ranks_group=None, ranks=[0],
        )
    # The guard must fire before any handle is exported.
    ipc_handler.export.assert_not_called()
```

见 [tests/test_p2p_guard.py:L30-L45](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_p2p_guard.py#L30-L45)。几个细节:`req_func=lambda _paths: None` 是无操作的请求回调(守卫先于一切通信,它根本不会被调到);`ranks_group=None` 直接跳过进程组参数;`match` 参数本身是 `re.search`,所以 `.*` 能跨过消息里含 ranks 列表的中间段。`export.assert_not_called()` 上方那行注释就是整个测试存在的理由。

被守卫保护的真实代码——fail loudly 而非深层不透明报错:

```python
if not self.device_manager.supports_device_ipc():
    raise RuntimeError(
        f"[rank{self._rank}] weight update requires cross-process device-tensor IPC, which "
        f"is not available for device type '{self.device_manager.device_type}' ...")
```

见 [checkpoint_engine/ps.py:L762-L772](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L762-L772),注释直言:与其在更深处炸出 `_share_fd_: only available on CPU` 这种天书,不如在入口就讲清楚。P2P 侧的守卫在 [ps.py:L779-L788](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L779-L788),并说明动机:Mooncake 对 XPU 显存没有 Level Zero 后端,注册必失败,不如明确拒绝并建议改用广播。

文件级文档——先讲清「为什么能纯 CPU」:

> CPU-only: we stub the ParameterServer internals up to the guard.

见 [tests/test_p2p_guard.py:L1-L6](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_p2p_guard.py#L1-L6)。这个 repo 的测试文件普遍带这种 docstring(test_ipc_handler.py、test_xpu_ipc.py 同款),说清测试边界(测什么、不测什么、靠什么替身)是仓库的测试写作惯例。

#### 4.5.4 代码实践

1. **实践目标**:为守卫补第三个用例,覆盖「广播路径 + IPC 不可用」的组合。
2. **操作步骤**:
   - 现有的 `test_ipc_unavailable_rejected` 传的是 `ranks=None`;仿照它写一个 `ranks=[]` 的变体(**示例代码**,加在本地分支):
     ```python
     # 示例代码(测试草稿,非项目原有代码)
     def test_ipc_unavailable_rejected_broadcast_empty_ranks():
         ps = _ps_with_device("xpu", supports_ipc=False, supports_p2p=True)
         ipc_handler = MagicMock()
         with (
             patch.object(dist, "is_initialized", return_value=True),
             pytest.raises(RuntimeError, match="cross-process device-tensor IPC"),
         ):
             ps._update_per_bucket(
                 "ckpt", req_func=lambda _paths: None,
                 ipc_handler=ipc_handler, ranks_group=None, ranks=[],
             )
         ipc_handler.export.assert_not_called()
     ```
   - 运行 `pytest tests/test_p2p_guard.py -v`。
3. **需要观察的现象**:新用例是否通过——关键是 `_update_per_bucket` 里 `if not ranks:` 的分支对 `[]` 与 `None` 是否同归广播路径(u1-l1 讲过 `ranks == None or []` 均为广播)。
4. **预期结果**:通过;IPC 守卫位于 ranks 分支**之前**([ps.py:L766](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L766) 在 [L776](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L776) 之前),两种 ranks 取值都应先撞上它。**待本地验证**。
5. 顺带观察:即使把 `supports_p2p=True` 传入,该用例也不会走到 P2P 分支——因为 IPC 守卫更靠前,这正是「守卫顺序」本身的可测性质。

#### 4.5.5 小练习与答案

**练习 1**:为什么用 `ParameterServer.__new__` 而不是 `MagicMock(spec=ParameterServer)` 来替身 PS?

**答案**:被调的是**真**方法 `ps._update_per_bucket`——测试要验证的是这段真实逻辑里的守卫;MagicMock 会把方法本身也 mock 掉,守卫永远不执行。`__new__` 保留类、只换掉「实例状态」,依赖则手工注入,是「测真方法 + 假依赖」的标准手法([tests/test_p2p_guard.py:L18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_p2p_guard.py#L18))。

**练习 2**:如果有人把 P2P 守卫从函数开头挪到 `ipc_handler.export()` 调用之后,这两个测试会怎么反应?

**答案**:`test_p2p_update_rejected_on_xpu` 里的 `ipc_handler.export.assert_not_called()`([tests/test_p2p_guard.py:L44-L45](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_p2p_guard.py#L44-L45))会失败——即使 RuntimeError 照样抛出。这条「未被调用」断言专门守住「先检查、后行动」的顺序契约,防止失败路径产生半成品副作用(泄漏的 IPC 导出句柄)。

**练习 3**:test_p2p_guard.py 与 test_update.py 都测 `_update_per_bucket`,两者的分工是什么?

**答案**:test_p2p_guard 测**边界条件**(不支持的设备组合应在入口被拒,纯 CPU、毫秒级);test_update 测**主干正确性**(支持的设备上端到端数值一致与错误传播,需要 GPU)。一个守门、一个验货,合起来才是这个函数的完整安全网。

## 5. 综合实践

**任务:给 api.py 的未覆盖端点补一组 CPU 测试,并跑通全套 CPU 门禁。**

背景:test_api.py 的 docstring 明说它只覆盖 metas 端点([tests/test_api.py:L1](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L1)),而 `_init_api` 一共暴露 7 个端点(u4-l5 的映射表)。你要把测试伞罩到其中的 register/unregister/healthz 上,完整走一遍「读被测代码 → 造替身 → 写断言 → 跑门禁」。

步骤:

1. **读被测代码**:精读 [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py) 中三个端点的定义,记下每个端点:HTTP 方法与路径、请求体模型(pydantic 字段)、调用了 PS 的哪个方法、什么参数、可能抛什么异常。
2. **造替身**:复用 test_api.py 的 `ps_mock` fixture;对 register 端点,用 `ps_mock.register_checkpoint.return_value = None` 即可;对错误路径,用 `side_effect` 注入 RuntimeError。
3. **写用例**(每个端点至少 1 条 happy path + 1 条失败路径):
   - healthz:200 且不触碰 ps_mock(可加 `ps_mock.get_metas.assert_not_called()` 之类的反向断言);
   - register:合法 JSON → 200 且 `assert_called_once_with` 参数正确;非法 JSON → 422 且 PS 未被调用;PS 抛错 → 500 且响应文本含异常消息;
   - unregister:200 且调用参数正确;PS 抛 KeyError → 500。
4. **跑门禁**:
   ```bash
   pytest tests/test_api.py -v               # 新用例
   pytest tests/ -m "not gpu"                # 全套 CPU 回归(与 CI 同口径)
   ```
5. **对照 CI**:把你本地 `pytest -v -m "not gpu" tests/` 的结果与 [.github/workflows/cpu-tests.yml:L28-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/.github/workflows/cpu-tests.yml#L28-L30) 的 CI 口径对齐——本地过、CI 不过的常见原因是本地装了 GPU 导致 `-m "not gpu"` 之外的行为差异,或本地缺 `.[p2p]` extra。

验收标准:新用例全部通过;全套 `-m "not gpu"` 无新增失败;每条用例都至少包含一个「状态码 + PS 方法调用方式」的双断言。本环境无法执行 pytest,运行结果**待本地验证**。

## 6. 本讲小结

- 测试体系由 `gpu` marker 切成两层:GPU 端到端(test_update、reuse_pin_memory、inplace_unpin、xpu_ipc、device_manager 的真 XPU 用例)与纯 CPU 单元层;CI 在无卡环境跑 `pytest -v -m "not gpu" tests/`,marker 注册在 [pyproject.toml:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169)。
- test_update.py 是**自举结构**:pytest 函数只当发射台,用 subprocess 拉起 `torchrun`,torchrun 回头执行同一文件的 `__main__` 分支;`PYTEST_CURRENT_TEST` 检查落实了「禁止直接 torchrun」;import 期的 `DeviceManager()` TypeError 兜底保证无卡机器也能收集。
- 端到端 mock 的核心思路是**真协议栈 + 假业务逻辑**:checker 子进程复用真实的 `update_weights_from_ipc`,只把 `run` 换成逐张量比对、`post_hook` 换成 synchronize;`req_func` 用 `queue.put` 顶替,`None` 毒丸终止循环;错误注入用例同时锁死协议两端的消息契约(`"Some workers failed to update weights"` / `"Failed to update weights due to remote errors"`)。
- `run_with_files` 用三路混合输入(/dev/shm 的 inplace pin、磁盘文件的 normal pin、内存张量)一次压测三条加载路径,checker 校验张量全集。
- CPU 替身有两种可模仿范式:接口层用 `MagicMock` + `TestClient` + `TypeAdapter` 严格往返(test_api.py);纯逻辑层用 `ParameterServer.__new__` + `SimpleNamespace` 手工装配,并用 `assert_not_called()` 守住「守卫先于副作用」的顺序契约(test_p2p_guard.py)。
- 写新测试的仓库惯例:文件头 docstring 说明测试边界与替身策略;硬件用例 fail loudly 而非静默 skip;`gpu` 与 `skipif` 双保险各司其职。

## 7. 下一步学习建议

- **下一讲 u6-l2(examples/update.py 完整编排解析)**:把视角从「怎么测」转回「怎么用」——你会看到本讲的 `req_func`/`queue.put` 在生产脚本中对应的真实实现 `req_inference`(HTTP 调 vLLM),以及 broadcast/p2p/all 三种更新方法的编排顺序。
- **延伸阅读(建议按序)**:
  1. [tests/test_xpu_parity.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py) —— 仓库里最丰富的 CPU mock 集合(monkeypatch、tmp_path、缓存失败重试、update 失败时句柄释放),是 4.4/4.5 两种范式的进阶版;
  2. [tests/test_rdma_parser.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py) —— 参数化驱动的不动点测试,看 `NCCL_IB_HCA` 语法如何被穷举;
  3. [.github/workflows/cpu-tests.yml](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/.github/workflows/cpu-tests.yml) —— CI 全文只有 30 行,理解「测试伞 = 一条命令」的极简主义。
- **动手建议**:完成第 5 节综合实践后,尝试给 `_gen_h2d_buckets` 或 `_assign_receiver_ranks` 的新边界(如单参数超桶、owner 集中在一个网卡)补一条参数化用例,对照 [tests/test_assign_receiver_ranks.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_assign_receiver_ranks.py) 的既有风格。
