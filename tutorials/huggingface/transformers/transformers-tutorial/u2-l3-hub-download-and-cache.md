# Hub 下载与本地缓存机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `from_pretrained("某个 repo id")` 时，一个远端文件是「如何被定位、下载、写盘、再命中」的完整链路。
- 读懂 `src/transformers/utils/hub.py` 中的 `cached_file` / `cached_files`，并解释单文件下载（`hf_hub_download`）与多文件下载（`snapshot_download`）的分流。
- 画出本地缓存目录的树状结构（`models--<org>--<name>` → `refs/`、`snapshots/<commit_hash>/`、`blobs/`），并解释「内容寻址（content-addressed）」与「符号链接农场（symlink farm）」如何让同一仓库的多个 revision 共享存储。
- 理解离线模式（`HF_HUB_OFFLINE` / `local_files_only`）与 `_commit_hash` 快速路径如何在无网或重复加载时跳过网络请求。
- 知道 `core_model_loading.py` 在整条链路中的位置：它是「下载之后的权重加载层」，而不是下载器本身。
- 知道 `dependency_versions_check.py` 是这一切之前的「运行时依赖守门」。

本讲承接 [u2-l2](u2-l2-from-pretrained-paradigm.md)。在 u2-l2 里我们说过：四大预训练对象共享 `from_pretrained` / `save_pretrained`，而其「读取底盘」是自由函数 `cached_file`。本讲就把这个底盘彻底拆开。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**（1）「repo id」就是仓库地址。** 当你写 `AutoModel.from_pretrained("google-bert/bert-base-uncased")`，字符串 `"google-bert/bert-base-uncased"` 就是一个 repo id：组织/用户名 `google-bert` + 模型名 `bert-base-uncased`。Hugging Face Hub 是一个基于 git 的模型托管服务，所以一个 repo 有分支（branch）、标签（tag）、提交（commit），三者统称为 **revision**。

**（2）下载是「文件级」的，不是「仓库级」的。** 加载一个模型并不需要把整个仓库的所有文件都拉下来，通常只需要 `config.json` + 若干权重分片。transformers 只下载它当前真正需要的文件，并把这些文件缓存到本地，下次再用时直接读本地。

**（3）缓存的核心思想是「内容寻址 + 符号链接」。** 每个文件按其内容的哈希值存成一个 blob；每个 revision 的每个文件只是指向某个 blob 的软链接。这样：同一文件在不同 revision 中只存一份；同一仓库的 `main` 分支和某个旧 commit 共享未变更的部分。

如果你还不熟悉 `from_pretrained` 的总体流程，建议先读 u2-l2，了解 config / model / tokenizer / processor 共享的加载范式。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲重点 |
|---|---|---|
| [src/transformers/utils/hub.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py) | **下载与缓存层**（本讲主角） | `cached_file` / `cached_files`、`try_to_load_from_cache`、离线模式、`_commit_hash` 快速路径 |
| [src/transformers/core_model_loading.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py) | **下载之后的权重加载层**（消费 hub.py 解析到的文件） | `convert_and_load_state_dict_in_model`：把磁盘上的权重灌进模型 |
| [src/transformers/dependency_versions_check.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_check.py) | **运行时依赖守门** | 在任何下载/加载发生前，校验关键依赖版本 |
| [src/transformers/dependency_versions_table.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_table.py) | 依赖版本号总表（自动生成） | `deps` 字典，所有版本约束的单一事实来源 |
| [src/transformers/modeling_utils.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py) | `PreTrainedModel.from_pretrained` 所在地 | 展示它如何构造 `cached_file_kwargs` 并反复调用 `cached_file` |

> 提示：注意区分两个 `utils`——仓库根目录的 `utils/` 是 CI/仓库脚本；运行时用的是 `src/transformers/utils/`。本讲的 `hub.py` 在后者。

---

## 4. 核心概念与源码讲解

### 4.1 utils/hub.py：下载与缓存的核心

#### 4.1.1 概念说明

`utils/hub.py` 是 transformers 与 Hugging Face Hub 之间的「胶水层」。它本身**几乎不实现网络下载**，真正的 HTTP 请求、文件落盘、缓存目录维护都委托给 `huggingface_hub` 库（`hf_hub_download`、`snapshot_download`、`try_to_load_from_cache` 等，见文件顶部的导入）。transformers 在这层胶水里做的是三件事：

1. **统一入口**：提供一个对所有上层（config / model / tokenizer / processor）都一致的 `cached_file(path_or_repo_id, filename)`，屏蔽「本地目录 vs 远端 repo」的差异。
2. **错误转译**：把 `huggingface_hub` 抛出的各类异常（`RepositoryNotFoundError`、`GatedRepoError`、`RevisionNotFoundError`、`LocalEntryNotFoundError` 等）翻译成带有人性化提示的 `OSError`。
3. **优化与降级**：用 `_commit_hash` 链式传递避免重复网络请求；在离线或断网时优雅地退化为「只读本地缓存」。

#### 4.1.2 核心流程

`cached_file` 是对外单文件入口，它只是把 `[filename]` 包成列表后转交给真正干活的 `cached_files`。`cached_files` 的执行流程可以概括为：

```
cached_files(path_or_repo_id, filenames, **kwargs)
│
├─ ① 离线模式检测：is_offline_mode() 为真 → 强制 local_files_only=True
├─ ② 本地目录快捷路径：path_or_repo_id 是本地目录 → 直接 os.path.join 返回，不联网
│
├─ ③ 确定 cache_dir：未指定则用 constants.HF_HUB_CACHE
├─ ④ _commit_hash 快速路径：若调用方传了 commit_hash 且非 force_download
│        → 对每个文件 try_to_load_from_cache，全部命中则直接返回（0 次网络请求）
│
├─ ⑤ 真正下载：
│        - 只有 1 个文件 → hf_hub_download(...)
│        - 多个文件      → snapshot_download(allow_patterns=filenames, ...)
│
├─ ⑥ 异常处理与降级：把 HfHub 异常转译成 OSError；
│        断网/离线类异常时，尝试用 try_to_load_from_cache 从缓存兜底
│
└─ ⑦ 收尾：再次 _get_cache_file_to_return 解析每个文件路径，缺文件按 flag 决定是否抛错
```

其中第 ④ 步是性能关键：当加载一个模型时，config、tokenizer、权重会**依次**调用 `cached_file`，如果把第一次解析出的 `commit_hash` 作为 `_commit_hash` 传给后续调用，后续调用就能直接命中缓存而无需再发任何 HTTP HEAD 请求。

#### 4.1.3 源码精读

**对外入口 `cached_file`：薄包装。** 它把单个 `filename` 包进列表，调用 `cached_files` 后取出第 0 个结果。它本身不做任何下载逻辑：[src/transformers/utils/hub.py:238-295](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L238-L295)。

```python
def cached_file(path_or_repo_id, filename, **kwargs) -> str | None:
    file = cached_files(path_or_repo_id=path_or_repo_id, filenames=[filename], **kwargs)
    file = file[0] if file is not None else file
    return file
```

**离线模式强制本地化。** 若 `huggingface_hub.is_offline_mode()` 返回真（通常因为设了 `HF_HUB_OFFLINE=1`），则把 `local_files_only` 强制为 `True`，后续走纯缓存路径：[src/transformers/utils/hub.py:378-382](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L378-L382)。

**本地目录快捷路径。** 如果 `path_or_repo_id` 实际是一个本地文件夹，就直接 `os.path.join` 拼路径并返回，完全不联网——这就是为什么 `from_pretrained("./本地目录")` 永远不需要网络：[src/transformers/utils/hub.py:388-404](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L388-L404)。

**确定缓存目录。** 没有显式传 `cache_dir` 时，回退到 `huggingface_hub.constants.HF_HUB_CACHE`：[src/transformers/utils/hub.py:406-409](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L406-L409)。`HF_HUB_CACHE` 由 `huggingface_hub` 的 `constants` 模块解析：优先取 `HF_HUB_CACHE` 环境变量，否则取 `HF_HOME/hub`，再否则取 `XDG_CACHE_HOME/huggingface`。这正是 u1-l2 提到的优先级链。

**`_commit_hash` 快速路径。** 调用方若传入了 commit hash，则对每个目标文件调用 `try_to_load_from_cache`；只要全部命中就直接返回，避免任何网络往返：[src/transformers/utils/hub.py:413-430](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L413-L430)。注意 `_CACHED_NO_EXIST` 这个哨兵值——它表示「此前确认过该文件不存在」，也算一种「命中」（命中了「不存在」这一事实）。

**下载分流。** 单文件用 `hf_hub_download`（更省），多文件用 `snapshot_download` 配合 `allow_patterns`：[src/transformers/utils/hub.py:434-464](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L434-L464)。

```python
if len(full_filenames) == 1:
    hf_hub_download(path_or_repo_id, filenames[0], ...)   # 单文件：更轻
else:
    snapshot_download(path_or_repo_id, allow_patterns=full_filenames, ...)  # 多文件：批量
```

**异常转译与缓存兜底。** 下载抛错后，函数先把 `RepositoryNotFoundError` / `RevisionNotFoundError` / `PermissionError` 等翻译成带提示的 `OSError`；对于断网类错误（`LocalEntryNotFoundError`），则尝试用 `_get_cache_file_to_return` 从本地缓存救回：[src/transformers/utils/hub.py:466-527](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L466-L527)。`_get_cache_file_to_return` 的本质就是再调一次 `try_to_load_from_cache`：[src/transformers/utils/hub.py:115-128](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L115-L128)。

**从缓存路径反推 commit hash。** 一个有趣的工具函数 `extract_commit_hash`，用正则 `snapshots/([^/]+)/` 从缓存文件路径里抠出 commit hash，用于在链式加载中传播版本信息：[src/transformers/utils/hub.py:224-235](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L224-L235)。这直接印证了缓存目录的 `snapshots/<commit_hash>/` 结构（见 4.1.4）。

**分片权重的批量下载。** 当模型权重被切成多个分片时，`get_checkpoint_shard_files` 先读 `index.json` 拿到所有分片文件名，再一次性交给 `cached_files` 批量下载：[src/transformers/utils/hub.py:851-909](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L851-L909)。

**下载参数的类型化。** `DownloadKwargs` 是一个 `TypedDict`，集中描述了所有下载相关的参数（`cache_dir`、`force_download`、`local_files_only`、`revision`、`_commit_hash` 等），被 `from_pretrained` 复用：[src/transformers/utils/hub.py:91-100](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L91-L100)。

#### 4.1.4 本地缓存目录结构

把上面这些代码拼起来，缓存目录长这样（这是 `huggingface_hub` 的标准布局，与 `extract_commit_hash` 的正则一一对应）：

```
<HF_HUB_CACHE>/                          # 默认 ~/.cache/huggingface/hub
└── models--google-bert--bert-base-uncased/   # repo 类型--org--name
    ├── refs/
    │   └── main                          # 文件内容 = main 指向的 commit hash
    ├── snapshots/
    │   ├── <commit_hash_A>/              # 某个 revision 的「视图」
    │   │   ├── config.json   -> ../../blobs/<sha-配置>
    │   │   └── model.safetensors -> ../../blobs/<sha-权重>
    │   └── <commit_hash_B>/              # 另一个 revision（如旧 tag）
    │       └── config.json   -> ../../blobs/<sha-配置>   # 同一 blob，复用！
    └── blobs/
        ├── <sha-配置>                    # 真正的文件内容（按内容哈希命名）
        └── <sha-权重>
```

要点：

- **`blobs/` 才是真实文件**，文件名是内容的哈希（内容寻址）。
- **`snapshots/<commit_hash>/` 是符号链接农场**，每个软链接指向 `blobs/` 里的某个 blob。`try_to_load_from_cache` 返回的就是 `snapshots/<commit_hash>/<filename>` 这条路径。
- **`refs/<branch>`** 存储分支名到 commit hash 的映射，让 `revision="main"` 能定位到具体的 `snapshots/` 子目录。
- 同一文件在两个 revision 里若内容相同，只占一份磁盘空间——这正是缓存高效的原因。

用一个极简的「模型」刻画缓存键的派生关系（缓存的逻辑地址由三元组 `(repo_type, repo_id, revision, filename)` 决定，物理地址由内容哈希决定）：

\[
\text{logical} = \texttt{snapshots}/H(\text{repo\_id},\text{revision})/\text{filename}
\quad\Rightarrow\quad
\text{physical} = \texttt{blobs}/\mathrm{sha}(\text{content})
\]

逻辑路径与物理 blob 之间通过符号链接解耦，于是「同一文件多 revision 共享」和「按 revision 隔离视图」得以同时成立。

#### 4.1.5 代码实践：观察单文件下载的缓存命中

**实践目标：** 用最小代码触发一次真实下载，肉眼确认 `cached_files` 各分支的行为。

**操作步骤：**

1. 准备一个干净环境并设缓存目录：

```bash
export HF_HOME=/tmp/hf_demo_cache
export HF_HUB_CACHE=/tmp/hf_demo_cache/hub
python -c "import transformers; print(transformers.__version__)"
```

2. 直接调用 `cached_file`，下载 `config.json`（最小文件）：

```python
# 示例代码
from transformers.utils.hub import cached_file

path = cached_file("google-bert/bert-base-uncased", "config.json")
print("解析到的缓存路径:", path)
```

3. 用 `tree` 或 `find` 观察目录结构（示例命令，需读者本机执行）：

```bash
find /tmp/hf_demo_cache/hub -maxdepth 4 | sort
ls -l /tmp/hf_demo_cache/hub/models--google-bert--bert-base-uncased/snapshots/*/
# 注意 config.json 是软链接 -> ../../blobs/<某 sha>
```

**需要观察的现象：**

- 第一次运行会打印下载进度条（由 `huggingface_hub` 产生）；`path` 形如 `.../snapshots/<40位hash>/config.json`。
- `snapshots/<hash>/config.json` 在 `ls -l` 下显示为软链接，指向 `../../blobs/<另一串hash>`。
- `refs/main` 文件的内容正是 `snapshots/` 下那个目录名。

**预期结果：** 缓存目录里出现 `models--google-bert--bert-base-uncased/{refs,snapshots,blobs}` 三级结构，且 `config.json` 是软链接。若网络不可用则行为待本地验证。

#### 4.1.6 小练习与答案

**练习 1：** 为什么 transformers 在「只下载 1 个文件」时用 `hf_hub_download`，而「多个文件」时改用 `snapshot_download`？

> **参考答案：** `hf_hub_download` 针对单文件优化，开销小；`snapshot_download` 能在一次调用里按 `allow_patterns` 批量拉取并统一管理 revision 与缓存，适合权重分片等多文件场景。源码中的分流见 [hub.py:434-464](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L434-L464)。

**练习 2：** `_CACHED_NO_EXIST` 这个哨兵值在 `_commit_hash` 快速路径里是如何被处理的？

> **参考答案：** 若 `try_to_load_from_cache` 返回 `_CACHED_NO_EXIST`，表示此前已确认该文件不存在。此时若 `_raise_exceptions_for_missing_entries=True` 则抛 `OSError`；否则计入 `file_counter`（视为「已处理」），见 [hub.py:419-426](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L419-L426)。

**练习 3：** `extract_commit_hash` 依赖缓存路径中的哪一段？这暗示了什么目录结构？

> **参考答案：** 它用正则 `snapshots/([^/]+)/` 抠出 commit hash，暗示缓存里存在 `snapshots/<commit_hash>/...` 这一级目录，见 [hub.py:224-235](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L224-L235)。

---

### 4.2 core_model_loading.py：下载之后的权重加载层

#### 4.2.1 概念说明

需要先澄清一个容易误解的点：`core_model_loading.py` **不是下载器**。它的模块文档字符串写得很清楚——「Core helpers for loading model checkpoints」（加载模型检查点的核心辅助工具）。它解决的是「**文件已经在磁盘上了，怎么把它们变成模型里的参数**」这个问题。

也就是说，它在整条链路里位于 `hub.py` 的下游：

```
from_pretrained(repo_id)
   │
   ├─ cached_file(...) ──→ 得到磁盘上的权重文件路径   ← hub.py 负责（本讲 4.1）
   │
   └─ 把权重文件读进内存、做键名/形状转换、灌进 nn.Module ← core_model_loading.py 负责（本节）
```

之所以在本讲提到它，是因为「下载与缓存」的最终目的就是喂给这一层；理解它的边界，才能完整理解 `from_pretrained`。

#### 4.2.2 核心流程

`core_model_loading.py` 的主入口是 `convert_and_load_state_dict_in_model`。它的职责可以概括为：

1. 拿到一个「原始 checkpoint 的 state_dict」（键名可能和模型对不上）和一个刚在 meta device 上建好的空模型。
2. 按 `weight_mapping`（一组 `WeightRenaming` / `WeightConverter` 规则）**重命名/转换**每一个键，例如把融合的 `qkv` 切成 `q`/`k`/`v`（`Chunk`），或把多个 expert 张量合并（`MergeModulelist`）。
3. 处理分布式（张量并行 `tp_plan`、DTensor 分片）、量化（`hf_quantizer`）、dtype 转换与 device_map 摆放。
4. 把转换后的张量逐个写进模型对应的模块参数里，并产出 `LoadStateDictInfo`（`missing_keys` / `unexpected_keys` / `mismatched_keys`）。

为了控制内存，它默认用一个大小为 `GLOBAL_WORKERS = min(4, cpu_count)` 的线程池做**异步物化**（`spawn_materialize`），把「从 safetensors 切片真正读进内存」这一步推迟到真正需要时。

#### 4.2.3 源码精读

**模块定位（文档字符串）。** 明确写着是「加载模型检查点」，且文件顶部直接 `import torch`，说明它是 PyTorch 专属的加载层：[src/transformers/core_model_loading.py:14](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py#L14) 与 [src/transformers/core_model_loading.py:31](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py#L31)。

**主入口签名。** 接收模型、原始 state_dict、加载配置（`LoadStateDictConfig`，内含 device_map / dtype / quantizer / weight_mapping 等）、`tp_plan` 与可选的磁盘 offload 索引：[src/transformers/core_model_loading.py:1458-1464](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py#L1458-L1464)。

**异步物化的线程数。** 注释解释了为什么用 4 个线程而不是更多——I/O 密集型任务线程太多反而变慢（16 线程可能慢一倍）：[src/transformers/core_model_loading.py:1219-1222](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py#L1219-L1222)。

**真正把张量写进模块。** `set_param_for_module` 负责定位子模块、检查形状、设置参数并打上 `_is_hf_initialized` 标记（防止后续 `_init_weights` 把刚灌好的权重再次随机初始化）：[src/transformers/core_model_loading.py:1324-1372](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py#L1324-L1372)。

> 本节只定位它在链路中的角色，权重的「重命名/转换」机制（`WeightRenaming` / `WeightConverter` / `ConversionOps`）属于更深入的模型加载主题，本讲不展开，留待后续模型基类相关讲义。

#### 4.2.4 代码实践：阅读型实践——跟踪一次加载的下落

**实践目标：** 通过源码导航，确认「下载」与「加载」是两个分离的阶段。

**操作步骤：**

1. 打开 [src/transformers/modeling_utils.py:655-664](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L655-L664)，找到 `cached_file_kwargs` 的组装。
2. 顺着 [modeling_utils.py:677](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L677) 的 `cached_file(...)` 调用，确认它产出 `resolved_archive_file`（一个**本地路径**）。
3. 在同文件中搜索 `resolved_archive_file` 的后续使用，你会看到它最终被读成 state_dict 并交给 `core_model_loading` 的加载函数。

**需要观察的现象：** `cached_file` 的返回值是一个字符串路径；这个路径之后才被「打开、读张量、灌进模型」。下载与加载在代码上是清晰分离的两个阶段。

**预期结果：** 你能在 `from_pretrained` 里画出 `repo_id → cached_file → 本地路径 → state_dict → convert_and_load_state_dict_in_model → 模型参数` 这条主线。

#### 4.2.5 小练习与答案

**练习 1：** 为什么说 `core_model_loading.py` 不是「下载器」？

> **参考答案：** 它的文档字符串自述为「Core helpers for loading model checkpoints」，输入是已经落盘的 state_dict，输出是把张量写进 `nn.Module`。所有网络下载都由 `huggingface_hub`（经 `hub.py`）完成。

**练习 2：** `GLOBAL_WORKERS` 为什么取 `min(4, cpu_count)`？

> **参考答案：** 权重物化是 I/O 密集型，线程过多反而因争抢与开销拖慢速度（注释举例 16 线程可能慢一倍），故保守取 4，见 [core_model_loading.py:1219-1222](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/core_model_loading.py#L1219-L1222)。

---

### 4.3 dependency_versions_check.py：运行时依赖守门

#### 4.3.1 概念说明

在任何下载、缓存、加载发生**之前**，transformers 会在 `import transformers` 时跑一遍依赖版本校验。这由 `dependency_versions_check.py` 完成。它的意义在于：模型加载链路强依赖 `huggingface_hub`、`tokenizers`、`safetensors` 等库的**特定版本区间**——比如缓存目录布局、`hf_hub_download` 的签名、`try_to_load_from_cache` 的行为都和 `huggingface_hub` 版本紧密相关。若用户装了不兼容的版本，应当**尽早、清晰地**报错，而不是等下载到一半才崩在一个莫名其妙的地方。

#### 4.3.2 核心流程

1. 从 `dependency_versions_table.py` 导入 `deps` 字典（这是由 `setup.py` 的 `_deps` 经 `make fix-repo` 自动生成的版本总表）。
2. 遍历 `pkgs_to_check_at_runtime`（一组「必须运行时校验」的关键包）。
3. 对每个包，用 `require_version_core(deps[pkg])` 校验已安装版本是否满足约束。
4. 对 `tokenizers` 与 `accelerate` 做特殊处理：仅当它们**已安装**时才校验（它们是可选依赖）。
5. 顺序约束：`tqdm` 必须先于 `tokenizers` 校验。

#### 4.3.3 源码精读

**导入版本总表。** `deps` 来自自动生成的 `dependency_versions_table.py`，单一事实来源其实是 `setup.py` 的 `_deps`：[src/transformers/dependency_versions_check.py:15-16](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_check.py#L15-L16)。

**需要运行时校验的关键包清单。** 注意里面包含了下载/缓存链路最依赖的几个：`huggingface-hub`、`safetensors`、`tokenizers`、`filelock`：[src/transformers/dependency_versions_check.py:25-37](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_check.py#L25-L37)。

```python
pkgs_to_check_at_runtime = [
    "python", "tqdm", "regex", "packaging", "filelock",
    "numpy", "tokenizers", "huggingface-hub", "safetensors",
    "accelerate", "pyyaml",
]
```

**校验循环与可选依赖豁免。** `tokenizers` / `accelerate` 在未安装时直接 `continue`，不强制；其余包严格校验：[src/transformers/dependency_versions_check.py:39-58](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_check.py#L39-L58)。

**对外校验函数。** 业务代码可调用 `dep_version_check(pkg, hint)` 做按需校验：[src/transformers/dependency_versions_check.py:61-62](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_check.py#L61-L62)。

**版本总表样例。** 在 `dependency_versions_table.py` 里可以看到本讲链路的关键约束，例如 `huggingface-hub` 与 `tokenizers` 的版本范围（这些版本的 API 正是 `hub.py` 依赖的）：[src/transformers/dependency_versions_table.py:21](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_table.py#L21)（`"huggingface-hub": "huggingface-hub>=1.5.0,<2.0"`）与 [src/transformers/dependency_versions_table.py:76](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_table.py#L76)（`tokenizers`）。

#### 4.3.4 代码实践：触发并观察一次依赖校验

**实践目标：** 看到「守门」实际发生。

**操作步骤：**

1. 在 Python 里手动调用校验（示例代码）：

```python
# 示例代码
from transformers.dependency_versions_check import pkgs_to_check_at_runtime
from transformers.dependency_versions_table import deps

for pkg in pkgs_to_check_at_runtime:
    print(f"{pkg:20s} -> {deps.get(pkg)}")
```

2. （可选）用一个故意不满足的约束触发报错，观察 `require_version_core` 的提示：

```python
# 示例代码：仅供理解，会抛错
from transformers.utils.versions import require_version_core
require_version_core("huggingface-hub>=999.0.0")  # 期望抛出 ImportError
```

**需要观察的现象：** 第 1 步打印出每个关键包的版本约束；第 2 步应抛出带可读提示的 `ImportError`/`ModuleNotFoundError`，指明需要的版本。

**预期结果：** 你能直观看到「下载/加载链路依赖的库」及其版本约束清单；故意写错约束时会得到清晰的版本报错。具体报错文案待本地验证。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `tokenizers` 和 `accelerate` 的校验要带「已安装才检查」的豁免？

> **参考答案：** 它们是可选依赖（torch 才是主依赖）。未安装时属于合法状态，不应报错；只有装了但版本不对时才需要提醒，见 [dependency_versions_check.py:41-54](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/dependency_versions_check.py#L41-L54)。

**练习 2：** `deps` 字典的「单一事实来源」是哪里？为什么 `dependency_versions_table.py` 不该手改？

> **参考答案：** 事实来源是 `setup.py` 的 `_deps`；`dependency_versions_table.py` 顶部注释说明它由 `make fix-repo` 自动生成，手改会被覆盖。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**在线下载 → 观察缓存 → 离线复用**」的完整闭环。这是本讲的实践任务。

**实践目标：** 验证 `cached_files` 的「在线下载」「`_commit_hash` 快速路径」「离线退化」三种行为，并亲手看到内容寻址缓存的结构。

**操作步骤：**

1. **准备一个干净缓存目录并在线加载一次**（示例代码）：

```python
# 示例代码：第 1 步——在线加载
import os
os.environ["HF_HOME"] = "/tmp/hf_demo_cache"          # 指定缓存根
# 注意：HF_HOME 需在 import transformers 之前设置，才能影响默认缓存路径
from transformers import AutoConfig

cfg = AutoConfig.from_pretrained("google-bert/bert-base-uncased")
print("加载成功，model_type =", cfg.model_type)
```

2. **观察缓存结构**（示例命令，需读者本机执行）：

```bash
find /tmp/hf_demo_cache/hub -maxdepth 4 | sort
cat /tmp/hf_demo_cache/hub/models--google-bert--bert-base-uncased/refs/main
ls -l /tmp/hf_demo_cache/hub/models--google-bert--bert-base-uncased/snapshots/*/
```

   预期：看到 `refs/main`（内容是 commit hash）、`snapshots/<同款hash>/config.json`（软链接）、`blobs/<另一串hash>`（真实文件）。

3. **开启离线模式再次加载，验证缓存命中**（示例代码）：

```python
# 示例代码：第 3 步——离线复用
import os
os.environ["HF_HOME"] = "/tmp/hf_demo_cache"
os.environ["HF_HUB_OFFLINE"] = "1"                     # 强制离线
from transformers import AutoConfig

cfg = AutoConfig.from_pretrained("google-bert/bert-base-uncased")
print("离线加载成功，命中本地缓存")
```

4. **（进阶）用 `cached_file` 直接验证快速路径**（示例代码）：

```python
# 示例代码：第 4 步——手动传 _commit_hash
from transformers.utils.hub import cached_file, try_to_load_from_cache
# 先取到 main 的 commit hash（来自 refs/main 文件），再作为 _commit_hash 传入
path = cached_file("google-bert/bert-base-uncased", "config.json", local_files_only=True)
print("local_files_only 解析路径:", path)
```

**需要观察的现象与预期结果：**

| 步骤 | 现象 | 说明 |
|---|---|---|
| 1 | 首次运行有下载进度条，随后成功 | 触发 `hf_hub_download`，文件落盘到 `blobs/` |
| 2 | 目录含 `refs/`、`snapshots/`、`blobs/`；`config.json` 是软链接 | 印证 4.1.4 的缓存结构 |
| 3 | 无任何网络请求，直接加载成功 | `is_offline_mode()` 为真 → `local_files_only=True` → `try_to_load_from_cache` 命中 |
| 4 | 即使 `local_files_only=True` 也能拿到路径 | 走的就是 `_commit_hash` 快速路径 / 缓存兜底 |

若你的环境无法访问 Hugging Face Hub，可改用任意一个已下载到本地的 checkpoint 目录（`from_pretrained("./本地目录")`），此时 `cached_files` 走 4.1.3 的「本地目录快捷路径」，完全离线——这部分行为稳定可复现，其余依赖网络的步骤待本地验证。

---

## 6. 本讲小结

- **`hub.py` 是下载/缓存胶水层**：`cached_file` → `cached_files` 是所有 `from_pretrained` 的读取底盘，单文件走 `hf_hub_download`、多文件走 `snapshot_download`。
- **真正的网络与缓存机制在 `huggingface_hub`**：transformers 只做入口统一、错误转译与降级兜底（`try_to_load_from_cache`）。
- **缓存采用内容寻址 + 符号链接农场**：`models--<org>--<name>/{refs,snapshots/<commit_hash>,blobs}`，同一文件跨 revision 共享存储。
- **`_commit_hash` 快速路径**让 config/tokenizer/权重的链式加载在首次之后几乎零网络开销。
- **离线模式**（`HF_HUB_OFFLINE` / `local_files_only`）会把流程强制退化为纯本地缓存读取。
- **`core_model_loading.py` 是下载之后的权重加载层**（不是下载器），`dependency_versions_check.py` 则是这一切之前的依赖守门——三者共同构成 `from_pretrained` 的完整支撑。

## 7. 下一步学习建议

- 想看 `cached_file` 如何被反复调用以解析权重文件（safetensors 单文件 / index / `.bin` 兜底），直接精读 [modeling_utils.py 的 from_pretrained 区段](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/modeling_utils.py#L645-L732)，这会自然过渡到 [u5-l2 PreTrainedModel 模型基类](u5-l2-pretrained-model-base.md)。
- 想深入「下载之后的权重转换与灌入」，重点读 `core_model_loading.py` 的 `WeightRenaming` / `WeightConverter` / `ConversionOps`，这部分将在模型基类与权重转换相关讲义展开（参见 [u7-l4 权重转换与 GGUF 加载](u7-l4-weight-conversion-and-gguf.md)）。
- 想了解缓存背后 `huggingface_hub` 的更多环境变量（`HF_HUB_OFFLINE`、`HF_ENDPOINT` 镜像、`HF_HUB_ETAG_TIMEOUT` 等），建议阅读 `huggingface_hub` 官方文档的「Environment variables」一节并在本机试验。
- 下一讲 [u3-l1 分词器基础与 PreTrainedTokenizerBase](u3-l1-tokenizer-base.md) 将离开「下载与加载」，进入第一个具体的预处理对象——分词器。
