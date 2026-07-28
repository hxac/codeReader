# 项目目录结构与双后端架构地图

## 1. 本讲目标

上一讲我们建立了对 TileRT 的整体认知：它是一个以「单 token 延迟（TPOT）」为优化标尺的超低延迟 LLM 推理运行时，真正的运行时大脑被编译进后端 `.so` 共享库。本讲要带你走进仓库，画一张「看见任何一个目录名就知道它干什么」的地图。

学完本讲，你应当能够：

- 说出 `tilert` 顶层包下 `benchmark / models / pd_vllm` 三个子包各自的职责，并能从目录名快速定位功能模块。
- 解释为什么 TileRT 要为 DeepSeek-V3.2 和 GLM-5 分别维护两个后端库 `libtilert_dsv32.so` 与 `libtilert_glm5.so`。
- 理解「一个 Python 进程只能加载一个后端」这条硬约束的根因，并知道违反它会抛什么错。
- 看懂 `deepseek_v3_2/ops` 与 `glm_5/_dsa_v32/ops` 的镜像对应关系，能回答「为什么两个模型要各自维护一份 ops 目录」。

## 2. 前置知识

在进入源码之前，先用三段话补齐本讲需要的几个概念。

**（1）PyTorch 自定义算子（custom op）与命名空间。** PyTorch 允许把 C++/CUDA 写好的函数注册成算子，注册时要归到一个命名空间下，例如 `torch.ops.tilert.foo()` 表示调用 `tilert` 命名空间下的 `foo` 算子。一个 `.so` 动态库在被加载时会把自己的算子「注册」到进程的全局算子表里。这一点是本讲理解「双后端为何不能共存」的关键。

**（2）共享库 `.so` 与动态加载。** Linux 下 `.so` 是动态共享库。Python 既可以用 `ctypes.CDLL` 把它加载进进程地址空间，也可以用 `torch.ops.load_library` 让 PyTorch 触发其中的算子注册入口。加载时机可以「懒」——用到才加载，而不是 `import tilert` 时就加载。

**（3）模型族（model family）。** TileRT 目前支持两个模型族：DeepSeek-V3.2 与 GLM-5/5.1。它们各自有不同的张量维度、不同的算子实现，因此被编译成两份独立的后端库。后续你会看到，这两个模型族在仓库里几乎是「镜像」的两套目录。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目说明；其中一段 NOTE 明确点出「两个后端、一个进程只加载一个」 |
| `pyproject.toml` | 包元信息与精确锁定的依赖（torch==2.11.0 等），是 ABI 绑定的证据 |
| `tilert/__init__.py` | 包入口；`_BACKENDS` 字典与 `load_backend()` 函数定义于此 |
| `tilert/tilert_init.py` | 调用 `torch.ops.tilert.tilert_init_op()` 初始化 C++ 运行时 |
| `tilert/models/deepseek_v3_2/ops/__init__.py` | DSv3.2 的 DSA 融合算子全局索引 |
| `tilert/models/glm_5/_dsa_v32/ops/__init__.py` | GLM-5 复用的 DSA 算子镜像（注意它的 docstring） |
| `tilert/models/glm_5/ops/__init__.py` | GLM-5 专属算子（如 `sparse_index_v3`） |

仓库顶层目录一览（`git ls-files` 的真实产出）：

```
TileRT/                          （仓库根）
├── README.md                    项目说明 / 安装 / 用法
├── pyproject.toml               包元信息 + 精确 ABI 依赖
├── requirements.txt / -dev.txt  依赖清单
├── Dockerfile                   官方镜像（CUDA 13.2 基础环境）
├── scripts/lint.sh              代码检查脚本
├── assets/                      logo / 性能图
└── tilert/                      ← 本讲主角：Python 驱动与模型组装层
    ├── __init__.py              包入口：load_backend() 在此定义
    ├── tilert_init.py           C++ 运行时初始化的 Python 包装
    ├── generate.py              CLI 入口：python -m tilert.generate
    ├── utils.py
    ├── benchmark/               性能基准测试套件
    │   ├── config.py            BenchMode、workload 与汇总表
    │   ├── short_prompt.py / coding_prompt.py / long_prompt.py
    ├── models/                  模型组装层
    │   ├── base.py              TileRTModule 抽象基类
    │   ├── common.py / deepseek_config.py / utils.py
    │   ├── preprocess/weight_converter.py   HF 权重 → 8 卡分片
    │   ├── deepseek_v3_2/       模型族 A（DeepSeek-V3.2）
    │   │   ├── generator.py / model_args.py / temp_var_indices.py
    │   │   ├── modules/         层组装：dsa/end2end/mla_v2/mlp/moe/mtp…
    │   │   ├── ops/             DSA 融合算子（29 个 .py）
    │   │   └── refs/kernel.py   参考实现（golden 校验）
    │   └── glm_5/               模型族 B（GLM-5/5.1）
    │       ├── generator.py / model_args.py / temp_var_indices.py / params.py
    │       ├── modules/         与 deepseek_v3_2/modules 镜像对应
    │       ├── _dsa_v32/        复用 DeepSeek 架构（DSA）的算子子包
    │       │   ├── model_args.py
    │       │   └── ops/         DSA 算子镜像（24 个 .py）
    │       └── ops/             GLM 专属算子（sparse_index_v3）
    └── pd_vllm/                 PD 分离部署（v0.1.5 新增）
        ├── decode_server.py / receive_server.py / prefill_connector.py
        ├── pd_router.py / transport.py / wire.py / oai_parser.py
        ├── engine_iface.py
        └── profiles/            模型无关 profile（base + dsv32/glm5/mla_nsa）
```

> 提示：这张树状图是本讲最重要的「成品」。如果你能在不看图的情况下，凭目录名说出每个子包的职责，本讲就达标了。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：目录结构与模块职责、双后端 `.so` 架构、单进程单后端约束。

### 4.1 目录结构与模块职责

#### 4.1.1 概念说明

TileRT 的复杂度集中在两个抽象层：

- **下层**：被编译进 `.so` 后端的 tile 级 C++ 运行时（通过 `torch.ops.tilert.*` 暴露给 Python）。
- **上层**：Python 对 8 张 B200 的模型组装、权重加载与解码编排。

仓库里的 Python 代码只负责「上层」。因此顶层包 `tilert` 被划分成几个职责非常清晰的子区域：`generate.py` 是 CLI 入口，`benchmark/` 负责性能测试，`models/` 负责模型组装，`pd_vllm/` 负责 PD 分离部署。这种「入口 / 测试 / 组装 / 部署」的四分法是阅读整个项目的导航坐标。

#### 4.1.2 核心流程

理解目录布局的逻辑流向：

1. 用户通过 CLI（`generate.py`）或程序化 API 进入。
2. CLI 调 `load_backend` 把对应 `.so` 注入进程。
3. 进入 `models/<模型族>/generator.py` 完成权重加载与解码。
4. `benchmark/` 与 `pd_vllm/` 是两条「上层用例」分支：前者测延迟，后者做分离部署。

#### 4.1.3 源码精读

包入口 `tilert/__init__.py` 用一段 docstring 直接交代了整体设计，值得先读：

这段 docstring 说明两件事：后端不在 `import` 时加载，而是按需选择；两个后端都会注册同一个 `tilert` 算子命名空间——[tilert/__init__.py:L1-L12](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L1-L12)：包入口的整段 docstring，点明「两个后端、不在 import 时加载、共用 tilert 命名空间」三个事实。

包入口在最后还导出了运行时初始化函数——[tilert/__init__.py:L84](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L84)：`from .tilert_init import tilert_init`，把 C++ 初始化入口挂到顶层包上。

而 `tilert_init.py` 只有几行，它调用后端注册的初始化算子——[tilert/tilert_init.py:L10-L18](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/tilert_init.py#L10-L18)：`tilert_init()` 调 `torch.ops.tilert.tilert_init_op()`，这就是「Python 上层 → C++ 后端」的握手点之一。注意：能调到 `torch.ops.tilert.*` 的前提是已经 `load_backend`，否则该命名空间不存在。

`pyproject.toml` 则用「精确锁版本」回应了「上层驱动下层」的 ABI 依赖——[pyproject.toml:L17-L28](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/pyproject.toml#L17-L28)：依赖被钉死为 `torch==2.11.0`、`transformers==4.46.3`、`tokenizers==0.20.3`，注释明确指出这是因为 wheel 是按精确 ABI 编译的，换版本会失配。

#### 4.1.4 代码实践

**实践目标**：用 `git ls-files` 亲手产出一份目录清单，验证上面那张树状图。

**操作步骤**：

1. 在仓库根目录运行：`git ls-files | sed 's#/[^/]*$##' | sort -u` 列出所有「出现过文件的目录」。
2. 按顶层包分组统计每个子包的文件数，例如统计 `models/` 下两套镜像模型各自的 `.py` 数量：

```bash
echo "deepseek_v3_2 modules:" && git ls-files 'tilert/models/deepseek_v3_2/modules/*.py' | wc -l
echo "glm_5 modules:"          && git ls-files 'tilert/models/glm_5/modules/*.py' | wc -l
```

**需要观察的现象**：`modules/` 下两个模型族的 `.py` 数量应当完全一致（各 7 个：`dsa / end2end / mla_v2 / mlp / moe / mtp / mtp_preprocess` 加 `__init__`，共 8 个文件），印证「镜像对应」。

**预期结果**：你会看到两个 `modules/` 目录文件一一对应；而 `ops/` 目录文件数不同（DSv3.2 有 29 个，GLM-5 的 `_dsa_v32/ops` 有 24 个），说明算子层并非完全对称——GLM-5 只镜像了它实际需要的那部分 DSA 算子，外加自己独有的 `glm_5/ops/`。

> 待本地验证：具体文件数可能随版本变化，以你本地 `git ls-files` 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果要给项目新增一个「模型导出」工具脚本，按本讲的目录逻辑，它应该放在哪里？

**参考答案**：放在 `tilert/models/preprocess/` 旁边或 `tilert/` 顶层工具区。因为权重相关的离线工具（如 `weight_converter`）已经在 `models/preprocess/` 下，新工具遵循「同职责聚拢」原则更易被发现。

**练习 2**：为什么 `tilert_init.py` 只有几行，却要单独成一个文件而不是合并进 `__init__.py`？

**参考答案**：它是对后端算子的薄包装，单独成文件便于被 `__init__.py` 显式 `from .tilert_init import tilert_init` 暴露为公开 API，同时把「运行时初始化」这一职责单独成块，方便后续扩展（如 `tilert_force_init`）。

### 4.2 双后端 .so 架构

#### 4.2.1 概念说明

TileRT 不是「一个通用引擎跑所有模型」，而是「每个模型族一个量身定制的后端」。仓库同时随附两份后端库：

- `libtilert_dsv32.so`：DeepSeek-V3.2 的 tile 级运行时。
- `libtilert_glm5.so`：GLM-5/5.1 的 tile 级运行时。

之所以要拆成两份，是因为两个模型的张量维度、注意力结构、专家路由策略都不一样，编译器为每个模型生成的 tile 分解与重叠方案也不同。虽然它们的算子在 Python 层「长得像」（都是 RMSNorm、MLA、MoE 那一套），但底层编译产物是两套独立的内核。

#### 4.2.2 核心流程

后端选择的数据流：

1. 用户传入 `model_type`（取值 `deepseek_v3_2` 或 `glm5`）。
2. `load_backend` 在 `_BACKENDS` 字典里查到对应的 `.so` 文件名。
3. 用 `ctypes.CDLL(RTLD_GLOBAL | RTLD_LAZY)` 把库加载进进程。
4. 用 `torch.ops.load_library` 触发其中的 `torch.ops.tilert.*` 算子注册。
5. 此后所有 `torch.ops.tilert.xxx()` 调用都落到这个后端。

#### 4.2.3 源码精读

模型族到后端库的映射是一个极简字典——[tilert/__init__.py:L43-L46](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L43-L46)：`_BACKENDS = {"deepseek_v3_2": "libtilert_dsv32.so", "glm5": "libtilert_glm5.so"}`，这是「双后端」架构的最直接证据。

README 里也专门有一段 NOTE 强调这一点——[README.md:L182-L186](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L182-L186)：明确写出 v0.1.5 提供两个独立后端库，且每个 Python 进程只加载其中一个，两个模型不能在同一解释器共存。

那么「两个模型各自维护一份 ops 目录」的根因是什么？看下面这两个文件的 docstring：

- [tilert/models/deepseek_v3_2/ops/__init__.py:L1](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/__init__.py#L1)：DSv3.2 算子包的 docstring 是 `"""Core operations for deepseek v3.2."""`。
- [tilert/models/glm_5/_dsa_v32/ops/__init__.py:L1](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/ops/__init__.py#L1)：**注意**——这个位于 `glm_5/` 下的算子包，它的 docstring 同样写的是 `"""Core operations for deepseek v3.2."""`！

这个「文档串帮」的细节是理解镜像的关键：GLM-5 的 `_dsa_v32` 子包（`_dsa_v32` 即「DeepSeek Architecture v3.2」）是 DSv3.2 算子集的一份镜像副本。两个模型族之所以各自维护一份 ops，是因为：

1. ops 内部最终调 `torch.ops.tilert.*`，而这些算子的具体实现由各自的后端 `.so` 提供——两个后端是分别编译的内核，不能混用。
2. 即便架构（DSA）相同，两个模型的 `algorithm` 枚举成员、权重别名形状、`device_sharding` 分片逻辑也可能存在差异，物理上必须分属不同的 Python 命名空间（`deepseek_v3_2.ops` vs `glm_5._dsa_v32.ops`）。
3. GLM-5 还有自己独有的算子，单独放在 `glm_5/ops/` 下——[tilert/models/glm_5/ops/__init__.py:L1-L7](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/ops/__init__.py#L1-L7)：`glm_5/ops/` 只导出 `sparse_index_topk_v3`，是 GLM 专属、DSv3.2 不需要的算子。

而 `_dsa_v32/model_args.py` 里甚至保留了一个 `arch_name = "deepseek_v3_2"` 的配置——[tilert/models/glm_5/_dsa_v32/model_args.py:L11-L15](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/model_args.py#L11-L15)：这个 dataclass 的 `arch_name` 仍是 `"deepseek_v3_2"`，说明 GLM-5 复用了 DSv3.2 的架构描述，作为其共享 DSA 路径的参数来源。

#### 4.2.4 代码实践

**实践目标**：亲手验证「镜像 + 各自独有」这一目录结构。

**操作步骤**：

```bash
# 1) 列出两套 modules 文件名，直观对比镜像
diff <(git ls-files 'tilert/models/deepseek_v3_2/modules/*.py' | xargs -n1 basename | sort) \
     <(git ls-files 'tilert/models/glm_5/modules/*.py'          | xargs -n1 basename | sort)

# 2) 列出两个 ops 目录的文件名，看哪些是镜像、哪些是各自独有
echo "=== dsv32 独有 ==="
comm -23 \
  <(git ls-files 'tilert/models/deepseek_v3_2/ops/*.py' | xargs -n1 basename | sort) \
  <(git ls-files 'tilert/models/glm_5/_dsa_v32/ops/*.py' | xargs -n1 basename | sort)
```

**需要观察的现象**：步骤 1 的 `diff` 应当没有输出（两套 modules 文件名完全一致）；步骤 2 会列出 DSv3.2 有而 GLM-5 镜像里没有的算子（如 `flash_sparse_mla.py`、`sparse_index.py`、`topk.py`、`qkv_rope.py` 等）。

**预期结果**：你会清楚地看到「modules 层完全镜像、ops 层部分镜像 + 各自专属」的格局。这正好解释了为什么不能把两套 ops 合并——它们对接的是两份不同的 `.so` 内核。

> 待本地验证：算子集合会随版本演进，以你本地实际输出为准；重点是观察「镜像 vs 独有」的对比方法，而非具体文件名。

#### 4.2.5 小练习与答案

**练习 1**：`glm_5/_dsa_v32/ops/__init__.py` 的 docstring 为什么写的是「deepseek v3.2」而不是「glm5」？这是 bug 吗？

**参考答案**：不是 bug。`_dsa_v32` 子包是 DSv3.2 算子集在 GLM-5 命名空间下的镜像副本，docstring 原样保留是为了说明「这套算子源自 DeepSeek 架构」。它提示读者：虽然文件在 `glm_5/` 下，但其算子语义对应 DSA 架构。

**练习 2**：如果将来新增第三个模型族（假设叫 `model_x`），按本讲的设计，需要新增哪些东西？

**参考答案**：需要 ① 在 `_BACKENDS` 字典加一项 `"model_x": "libtilert_mx.so"`；② 新增 `tilert/models/model_x/` 目录（含 `generator / model_args / modules / ops`）；③ 一份对应的后端 `.so`，注册同样的 `tilert` 算子命名空间。它依然受「单进程单后端」约束。

### 4.3 单进程单后端约束

#### 4.3.1 概念说明

「双后端」带来一条硬约束：**一个 Python 进程只能加载一个后端**。这并非工程偷懒，而是由 PyTorch 自定义算子的注册机制决定的——两个后端都会把算子注册到同一个 `tilert` 命名空间下，如果两个 `.so` 同时加载，就会发生命名空间冲突（后注册的会覆盖先注册的，行为不可预期）。因此 TileRT 选择「显式拒绝」：检测到要加载第二个不同的后端时直接抛错，并提示你「换一个新进程」。

#### 4.3.2 核心流程

`load_backend` 的互斥判定逻辑：

```
输入 model_type
  ├─ 查不到 → ValueError
  ├─ _loaded_backend 已是同一个 .so → 直接 return（幂等，允许重复调用同一个）
  ├─ _loaded_backend 是另一个 .so → RuntimeError（拒绝）
  └─ 还没加载过 → ctypes.CDLL + torch.ops.load_library → 记录 _loaded_backend
```

注意「幂等」这一细节：对同一个后端重复调用 `load_backend` 是安全的，只是直接返回；只有「想换一个不同的后端」才被禁止。

#### 4.3.3 源码精读

互斥状态用模块级变量保存——[tilert/__init__.py:L48](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L48)：`_loaded_backend: str | None = None`，记录当前进程已加载哪个后端（初始为空）。

互斥判定的核心代码——[tilert/__init__.py:L62-L68](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L62-L68)：`if _loaded_backend is not None:` 之后，若 `_loaded_backend != so_name` 则抛 `RuntimeError`，提示「换一个新进程跑」；若相同则 `return`（幂等）。

真正的加载动作——[tilert/__init__.py:L76-L78](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L76-L78)：`ctypes.CDLL(str(lib_path), mode=ctypes.RTLD_GLOBAL | os.RTLD_LAZY)` 把符号以「全局 + 懒解析」模式注入进程，紧接着 `torch.ops.load_library` 触发算子注册，最后把 `so_name` 记进 `_loaded_backend`。

抛错信息也设计得很友好，直接告诉你怎么做：

```python
raise RuntimeError(
    f"TileRT backend '{_loaded_backend}' already loaded; cannot load "
    f"'{so_name}' in the same process. Run {model_type} in a fresh process."
)
```

> 关键直觉：`RTLD_GLOBAL` 让 `.so` 的符号全局可见，这正是两个后端会「撞车」的物理原因——它们都要往全局的 `tilert` 算子命名空间里注册同名算子。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次「双后端冲突」，看报错长什么样，并验证幂等行为。

**操作步骤**（在已安装 `tilert` 的环境里）：

```python
# 文件名：probe_dual_backend.py（示例代码，非项目原有文件）
import tilert

# 第一次：正常加载
tilert.load_backend("deepseek_v3_2")
print("第一次加载成功")

# 幂等：重复加载同一个，应当不报错
tilert.load_backend("deepseek_v3_2")
print("重复加载同一个后端：OK（幂等）")

# 冲突：换一个不同的后端，应当抛 RuntimeError
try:
    tilert.load_backend("glm5")
except RuntimeError as e:
    print("捕获到预期错误：", e)
```

运行：`python probe_dual_backend.py`

**需要观察的现象**：前两次打印正常；第三次进入 `except`，错误信息会建议你「Run glm5 in a fresh process」。

**预期结果**：你会直观体会到「单进程单后端」不是文档口号，而是代码里硬实现的护栏。要跑 GLM-5，必须另起一个进程。

> 待本地验证：本实践需要真实的 `libtilert_*.so` 后端库与匹配的 CUDA 环境（8× B200、torch 2.11.0+cu130）。在纯阅读型环境里，你可以只阅读上面 `L62-L68` 的判定逻辑，推断出同样的结论，不必真的跑起来。

#### 4.3.5 小练习与答案

**练习 1**：为什么互斥判定放在 `load_backend` 内部，而不是用操作系统级的文件锁？

**参考答案**：因为冲突发生在「同一进程的算子命名空间」层面，而非「文件被占用」层面。文件锁只能防止别的进程改文件，挡不住同一个进程里第二个 `.so` 往 `torch.ops.tilert` 注册同名算子。所以必须在进程内、在加载前用 `_loaded_backend` 做软互斥。

**练习 2**：如果某天 PyTorch 支持给算子命名空间加版本前缀（如 `tilert_dsv32::*` 与 `tilert_glm5::*`），这条约束是否还有必要？

**参考答案**：从技术上讲，若命名空间能彻底隔离，理论上可以在同进程加载两个后端。但 TileRT 当前所有 ops 硬编码调用 `torch.ops.tilert.*`，改成带前缀的命名空间需要改动两个模型族的所有算子，工程成本高且收益有限（生产部署本就把不同模型分到不同进程），所以现状是合理的架构取舍。

## 5. 综合实践

把本讲三个模块串起来，完成一张「双后端架构地图」文档。

**任务**：在一个 Markdown 文件里产出以下三样东西：

1. **目录树**：用 `git ls-files` 产出 `tilert/` 的三层树状结构，标注 `deepseek_v3_2` 与 `glm_5` 之间哪些子模块是镜像对应（至少覆盖 `modules/`、`ops/`、`_dsa_v32/ops/`、`ops/`、`generator.py`、`model_args.py`）。
2. **镜像关系表**：列一个三列表（DSv3.2 路径 / GLM-5 镜像路径 / 关系说明），明确写出哪些是完全镜像、哪些是部分镜像、哪些是各自独有。
3. **设计说明**：用 200 字以内回答——「为什么两个模型要各自维护一份 ops 目录？」要求至少覆盖：① ops 对接各自的后端 `.so`；② `_dsa_v32` 的含义与 docstring 证据；③ GLM-5 独有算子（`glm_5/ops/`）的存在。

**检查清单**：

- [ ] 你引用的 `_BACKENDS` 字典是否来自 `tilert/__init__.py:43-46`？
- [ ] 你是否说明了「同进程加载第二个后端会抛 `RuntimeError`」并指向 `L62-L68`？
- [ ] 你是否指出了 `glm_5/_dsa_v32/ops/__init__.py` docstring 写的是「deepseek v3.2」这一证据？

完成这份文档后，你就拥有了一份可长期维护的「TileRT 仓库导航图」。

## 6. 本讲小结

- TileRT 的 Python 代码只负责「上层」：CLI 入口（`generate.py`）、模型组装（`models/`）、基准（`benchmark/`）、PD 分离部署（`pd_vllm/`）。
- 真正的运行时大脑编译进两份后端库：`libtilert_dsv32.so` 与 `libtilert_glm5.so`，由 `_BACKENDS` 字典映射。
- 两个模型族在 `modules/` 层几乎完全镜像，在 `ops/` 层部分镜像（GLM-5 的镜像放在 `_dsa_v32/ops/`，且 docstring 自报家门为「deepseek v3.2」）。
- ops 目录之所以各自独立，是因为 ops 对接各自的后端 `.so` 内核，且权重别名 / 分片逻辑可能不同，不能合并。
- 「单进程单后端」是一条硬约束：`load_backend` 用 `_loaded_backend` 做软互斥，换不同后端会抛 `RuntimeError`，同一后端重复调用幂等。
- 这条约束的物理根因是两个后端共用 `tilert` 算子命名空间，且 `.so` 以 `RTLD_GLOBAL` 加载。

## 7. 下一步学习建议

本讲只画了「地图」，还没有真正「上路」。建议按以下顺序继续：

1. **先学安装与加载（u1-l3）**：动手把后端 `.so` 加载进进程，理解 `ctypes.CDLL` + `torch.ops.load_library` 的注册原理，把本讲的 `load_backend` 跑通。
2. **再学 CLI 与生成（u1-l4 / u1-l5）**：从 `generate.py` 进入，看一次端到端生成是怎么调用 `models/<模型族>/generator.py` 的。
3. **进阶时回到 `models/`**：当你需要理解模型组装时，再深入 `models/base.py` 的 `TileRTModule` 抽象体系，以及 `deepseek_v3_2/ops/` 里某个具体融合算子（如 `rmsnorm_projq_wqi.py`）。

一句话锚点：**本讲给你的是「目录名 → 职责」的查表能力；后续讲义给你的是「函数 → 行为」的纵深能力。**
