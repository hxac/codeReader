# ShowHandsDSALayer：8 卡多线程权重加载与 prepare_money 编排

## 1. 本讲目标

本讲聚焦 TileRT 把「一棵用 Python 装配出来的模型树」真正搬上 8 张 B200 并交给 C++ 后端的最后一公里。读完本讲，你应当能够：

- 说清 `ShowHandsDSALayer` 在 Generator 生命周期里扮演什么角色、它何时昂贵何时廉价。
- 读懂 `_init_weights` 中「为 8 张卡各起一个线程并行加载分片权重」的全过程，理解 `*_dev_{id}` 键名过滤为什么能让 8 个线程互不干扰。
- 解释 V2 P2P 中 `peer_bufs` 与 `ll_buf` 的指针回填，并说清它为什么必须排在「所有线程 join 之后、`prepare_money` 之前」。
- 理解 `prepare_money` 这一「把 Python 张量交给后端」的绑定契约：`params / temp_vars / caches / profile_logs` 四个扁平张量列表是如何从一棵 `Dsa` 树压扁出来的。

本讲是 u2 单元的枢纽：它把 u2-l1 的 `TileRTModule` 抽象、u2-l2 的 `ModelArgs` 超参，落实为一次「上电即用」的运行时初始化，并为 u2-l4（层组装）、u2-l5（三层张量契约）打下基础。

## 2. 前置知识

### 2.1 扑克隐喻：show hands / prepare money / go home

TileRT 的作者用一整套扑克术语为运行时控制算子命名，记住这套隐喻就能猜出每个算子的作用：

| 算子名 | 字面含义 | 运行时含义 |
|---|---|---|
| `dsa_show_hands_prepare_money` | 备好筹码 | 绑定张量、捕获 CUDA Graph（一次） |
| `dsa_show_hands` | 摊牌 / 出牌 | 跑一步解码 forward（每 token） |
| `dsa_show_hands_reset` | 开新局 | 复位 KV 缓存、开始新序列 |
| `dsa_show_hands_go_home` | 收工回家 | 释放图与后端资源 |

`ShowHandsDSALayer` 就是「牌桌管家」：它负责在开局前 `prepare_money`（把筹码=张量摆好），每步 `show_hands`（出牌=解码），结束时 `go_home`（收摊）。

### 2.2 C++ 后端不认识 Python 对象

回顾 u1-l3：真正的运行时大脑编译在 `libtilert_dsv32.so` 里，Python 只是把算子注册到 `torch.ops.tilert.*` 命名空间。C++ 后端**不理解** `Dsa`、`MoeBlock` 这些 Python 类，它只接受**扁平的张量列表**。因此必须有一层「翻译」：把树状的模型对象压扁成几个有序列表。`ShowHandsDSALayer` 就是这层翻译。

### 2.3 四类张量的职责

| 名称（代码里） | 别名 | 生命周期 | 内容 |
|---|---|---|---|
| `params` | 权重 | 整个模型常驻 | 模型参数（RMSNorm、投影、专家权重等） |
| `temp_vars` / `intermediates` | 激活 | 每步覆写 | 单步前向的临时激活、采样配置、token 输出 |
| `caches` | KV 缓存 | 整个序列累积 | 跨 token 持久化的 KI/KV/PE 缓存 |
| `profile_logs` | 性能日志 | 持续追加 | tile 级运行时的 profiling 缓冲区 |

### 2.4 前置讲义承接

- **u2-l1**：`TileRTModule` 是所有算子与容器的基类；`SerializableTileRTModule` 用 `exec_seq` 装配子算子，`init_tilert_weights` 按 `prefix + alias + suffix` 匹配权重，`get_weights_list` / `get_cache_vars` 递归汇总。本讲大量调用这些方法。
- **u2-l2**：`ModelArgs` 是超参单一事实来源，`num_devices` 固定为 8，`index_topk=2048` 等字段直接决定 `ll_buf` 大小。
- **u1-l5**：Generator 构造 `ShowHandsDSALayer` 但**不加载权重**；`from_pretrained` 才触发真正的 8 卡加载。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [tilert/models/deepseek_v3_2/modules/end2end.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py) | **本讲主角**。`ShowHandsDSALayer` 类与 `dsa_show_hands_*` 一组控制算子、`_init_weights` 多线程加载、`dsa_show_hands_prepare_money` 绑定入口。 |
| [tilert/models/deepseek_v3_2/modules/dsa.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py) | `Dsa` 容器。在 `__init__` 里创建 `v2_peer_bufs` / `v2_ll_buf`，提供 `get_weights_list` / `get_cache_vars` / `get_temp_vars` 三个压扁入口。 |
| [tilert/models/base.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py) | `SerializableTileRTModule`。`init_tilert_weights` 的键名匹配与用完即删（`remove_selected`）逻辑在此。 |
| [tilert/models/deepseek_v3_2/temp_var_indices.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py) | `Idx` 枚举（56 个激活槽）与 `validate_temp_vars_layout` 布局校验。 |
| [tilert/models/deepseek_v3_2/generator.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py) | `DSAv32Generator`。把 `ShowHandsDSALayer` 当作 `self.decode_layer` 使用，串起构造→`init`→`from_pretrained`→`generate`→`cleanup`。 |

## 4. 核心概念与源码讲解

### 4.1 多线程分卡权重加载

#### 4.1.1 概念说明

TileRT 跑在 8 张 B200 上，权重在离线阶段（u1-l6）已被 `WeightConverter` 切成「每卡一份」的布局，每个张量都带 `*_dev_{id}` 后缀（id = 0..7）。这意味着：

- **8 张卡的权重互不重叠**：卡 0 只需加载 `*_dev_0` 的键，卡 1 只需 `*_dev_1`，依此类推。
- **加载天然可并行**：因为 8 份分片分别落盘、分别命名，8 个线程可以同时各读各的，互不踩踏。

`_init_weights` 正是利用这一点：为每张卡起一个 Python `threading.Thread`，各自调用 `load_device_weights` 把本卡的 safetensors 分片读进 `cuda:{device_id}`，再就地装配成一棵 `Dsa` 树并压扁成四元组。

> 为什么用线程而非进程？因为 8 张卡共享同一个进程的 `torch.ops.tilert.*` 命名空间与 CUDA 上下文；`threading` 配合 `torch.cuda.device(device_id)` 上下文管理器即可把每个线程的工作钉在对应 GPU 上，避免进程间通信开销。

#### 4.1.2 核心流程

每个线程（`device_id` 从 0 到 7）执行以下数据流：

```
__load_weights(device_id, model_path)
  │
  ├─ load_device_weights(model_path, device_id, extra_keys, skip_keys)
  │     · 读 model.safetensors.index.json
  │     · 过滤出所有 *_dev_{device_id} 的键
  │     · 追加 extra_keys（embed_tokens / lm_head / norm，全局共享）
  │     · 按 skip_keys 剔除已缓存键（可选）
  │     · 逐文件 load_file / safe_open → cuda:{device_id}
  │     · 注入 freqs_cis（RoPE 频率表）
  │     → state_dicts: dict[str, Tensor]
  │
  ├─ Dsa(model_args, device_id, num_devices, cached_ffn_ops)
  │     · 装配 61 层（3 dense + 58 MoE）+ RMSNormHeadProj
  │     · 创建 v2_peer_bufs (dev 0) 或 v2_ll_buf (dev 1..7)
  │     → dsa
  │
  ├─ dsa.init_tilert_weights(state_dicts)
  │     · 按 prefix+alias+suffix 把权重分发到各叶子算子
  │     · 用完即删，降低峰值显存
  │
  ├─ params  ← dsa.get_weights_list()      # 递归汇总权重
  ├─ caches  ← dsa.get_cache_vars()        # 递归汇总 KV 缓存
  ├─ 记录 v2_p2p 指针                       # 见 4.2
  │
  ├─ intermediates ← generate_params_with_continuous_storage(
  │                     dsa.get_temp_vars(1, 4, sampling_args), device_id)
  │     · 56 个激活槽拼进一块连续显存
  │
  ├─ intermediates[Idx.SAMPLING_CONFIG].copy_([T, top_p, top_k, use_topp])
  │
  ├─ （可选）MTP 模块：扩展 params / caches
  │
  ├─ profile_logs ← get_profile_log_tensor(device_id, num_max_insts=65536)
  │
  └─ multi_devices_results[device_id] = (intermediates, caches, params, profile_logs)
```

四元组的类型在文件顶部就钉死了——`intermediates / caches / params / profile_logs`，对应 §2.3 的四类张量：

[tilert/models/deepseek_v3_2/modules/end2end.py:26](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L26) 定义每卡返回结果的类型别名 `DeviceResult`，是 `(intermediates, caches, params, profile_logs)` 四元组，对应 `temp_vars / caches / params / 日志` 四类张量。

#### 4.1.3 源码精读

**(a) 构造：廉价，不加载权重**

[tilert/models/deepseek_v3_2/modules/end2end.py:180](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L180) 构造时第一件事是 `validate_temp_vars_layout()`，校验 Python 侧 56 个激活槽枚举与后端 `dsa_temp_vars_size()` 一致，这是「Python 与 C++ 的契约校验」。

[tilert/models/deepseek_v3_2/modules/end2end.py:188-L189](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L188-L189) 把 `num_devices` 硬编码为 8，`forward_max_seq_len` 固定为 4（MTP 一次最多预测 4 个 token）。

[tilert/models/deepseek_v3_2/modules/end2end.py:195-L196](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L195-L196) 预分配两个长度为 `torch.cuda.device_count()` 的列表：`multi_devices_results` 存每卡的四元组，`_dsa_objects` 存每卡的 `Dsa` 树（供后续缓存复用）。构造函数只记配置、不碰磁盘，所以是廉价的。

**(b) `load_device_weights`：键名过滤是并行的关键**

[tilert/models/deepseek_v3_2/modules/end2end.py:214-L220](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L214-L220) 读 `model.safetensors.index.json`，只挑出键名以 `dev_{device_id}` 结尾的张量，再追加 `extra_keys`（embedding 与 head/norm 是全局共享，不带 dev 后缀）。这一行是「8 线程互不干扰」的根本原因：每个线程看到的键名集合天然不重叠。

[tilert/models/deepseek_v3_2/modules/end2end.py:222-L224](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L222-L224) 支持 `skip_keys` 剔除——配合 `cached_ffn_ops` 复用 MoE/MLP 算子时跳过已缓存的专家权重。

[tilert/models/deepseek_v3_2/modules/end2end.py:232-L248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L232-L248) 两种加载策略：有 `skip_keys` 时用 `safe_open` 逐键选择加载（省显存），否则整文件 `load_file`。每个文件读完都 `torch.cuda.empty_cache()`，控制峰值显存。

[tilert/models/deepseek_v3_2/modules/end2end.py:250](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L250) 把 RoPE 频率表 `freqs_cis` 注入 `state_dicts`——它不是从 checkpoint 读的，而是按 `ModelArgs` 现算的（见 `_gen_freqs_cis` 第 203-205 行）。

**(c) `__load_weights`：单卡装配**

[tilert/models/deepseek_v3_2/modules/end2end.py:360-L376](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L360-L376) `with torch.cuda.device(device_id)` 把当前线程钉到对应 GPU；随后调 `load_device_weights`，`extra_keys` 里特别包含 `layer_{n_layers}_lm_head.weight_dev_{device_id}` 与 `layer_{n_layers}_model.norm.weight_dev_{device_id}`——head/norm 借用 MTP 层号（= `n_layers`）走特殊路径，承接 u1-l6 的转换约定。

[tilert/models/deepseek_v3_2/modules/end2end.py:383-L392](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L383-L392) 构造 `Dsa`、调 `init_tilert_weights(state_dicts)` 把权重分发到叶子算子，再用 `get_weights_list()` / `get_cache_vars()` 递归压扁出 `params` 和 `caches`。

`init_tilert_weights` 的匹配与删除逻辑在基类里（u2-l1 已讲），关键是 [tilert/models/base.py:320-L341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L320-L341)：对每个子算子按 `prefix + alias + suffix` 取权重，`remove_selected=True` 时用完即删，`retain_weights=True`（仅 RMSNormHeadProj）则保留 head 权重供多卡共享。

**(d) 连续存储与采样配置写入**

[tilert/models/deepseek_v3_2/modules/end2end.py:402-L416](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L402-L416) 把 `dsa.get_temp_vars(1, forward_max_seq_len=4, sampling_args)` 产出的 56 个激活槽，用 `generate_params_with_continuous_storage` 拼进一块连续显存，作为 `intermediates`。

[tilert/models/deepseek_v3_2/modules/end2end.py:418-L430](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L418-L430) 把 `(temperature, top_p, top_k, use_topp)` 拷进 `intermediates[Idx.SAMPLING_CONFIG]`——采样参数不是函数入参，而是写进激活槽里由后端读取。

[tilert/models/deepseek_v3_2/modules/end2end.py:456-L460](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L456-L460) 生成 `profile_logs` 张量，组装出四元组 `result`，存进 `multi_devices_results[device_id]`，并记录 `_base_params_count` / `_base_caches_count`（不含 MTP 的部分，供 MTP 模式下二次 `prepare_money` 用）。

**(e) 线程编排与异常收口**

[tilert/models/deepseek_v3_2/modules/end2end.py:472-L489](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L472-L489) 起 8 个线程，每个线程套 `_runner` 把异常捕获进 `exceptions[dev_id]` 而不是直接抛——`threading` 不会把子线程异常冒泡到主线程，所以必须显式收集；`join` 之后再统一抛 `RuntimeError`，确保任一卡失败都能被发现。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，画出 8 个线程中任意一个（例如 `device_id=3`）的完整数据流。

**操作步骤**：

1. 打开 [tilert/models/deepseek_v3_2/modules/end2end.py:354](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L354)，从 `__load_weights(3, model_path)` 开始追踪。
2. 记下每一步产出的变量名与类型：`state_dicts` → `dsa` → `params` / `caches` → `intermediates` → `result`。
3. 对照 §4.1.2 的伪代码流程图，把 `device_id=3` 的具体值填进去（例如 `load_device_weights` 会过滤出所有 `*_dev_3` 的键）。
4. 检查 `extra_keys`（第 370-374 行）：embed_tokens 没有 `_dev_3` 后缀，而 lm_head/norm 有，解释为什么二者不同。

**需要观察的现象**：

- `device_id=3` 的 `state_dicts` 里**不包含**任何 `*_dev_0`、`*_dev_1` 等其他卡的键（除非是全局共享的 embed_tokens）。
- `params` 列表的长度 = 该卡所有叶子算子权重数量之和；`caches` 长度 = 该卡所有 MLA 层的 KI/KV/PE 缓存数量之和。

**预期结果**：8 个线程跑完后，`self.multi_devices_results` 是一个长度 8 的列表，每个元素都是一个 `(intermediates, caches, params, profile_logs)` 四元组，且 8 个四元组里的张量分别落在 `cuda:0` ~ `cuda:7` 上。

> 待本地验证：上述张量落点需要真实 8 卡环境验证。无 GPU 时只能做源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `self.num_devices` 从 8 改成 4，`load_device_weights` 还能正常工作吗？为什么？

**参考答案**：不能正常工作。离线转换（u1-l6）已经把权重按 `num_devices=8` 切成了 `*_dev_0` ~ `*_dev_7` 八份并落盘。改成 4 后，循环 `range(4)` 只会加载 `dev_0..dev_3`，`dev_4..dev_7` 的分片被丢弃，模型残缺；而且每张卡拿到的分片大小也不对。`num_devices` 是贯穿「转换 → 加载 → 通信」的全局不变量，不能单独改动。

**练习 2**：`load_device_weights` 在没有 `skip_keys` 时用 `load_file`（整文件加载），有时用 `safe_open`（逐键加载）。为什么要区分？

**参考答案**：`safe_open` 逐键加载允许只取需要的键、跳过其他键，适合 `cached_ffn_ops` 复用专家权重时跳过已缓存键，省显存；但逐键访问更慢。没有 `skip_keys` 时整文件 `load_file` 一次性读入更快。二者之后都调 `torch.cuda.empty_cache()` 控制峰值。

---

### 4.2 V2 P2P：peer_bufs 与 ll_buf 的指针回填

#### 4.2.1 概念说明

回顾 u2-l2 / u2-l6（预告）：MLA 注意力在 0 卡做 NSA 稀疏选择——0 卡对每个 query 选出 top-k 个最相关的历史位置，**其余 7 张卡需要拿到这份选择结果**才能各自算自己负责的那部分注意力头。这就需要一次跨卡通信。

TileRT 的 V2 方案不走传统的 allreduce，而是用 **CUDA P2P（peer-to-peer）直接写**：0 卡把稀疏选择结果**直接写**到其余 7 张卡预先注册好的接收缓冲区 `ll_buf` 里。要做到这一点，0 卡必须知道另外 7 张卡的 `ll_buf` 在它们各自 GPU 显存里的地址——这就是 `peer_bufs` 的作用。

| 缓冲区 | 谁创建 | 谁拥有 | 作用 |
|---|---|---|---|
| `v2_ll_buf` | 卡 1..7 各自 | 接收端 | 预留显存，接收 0 卡 P2P 写来的稀疏选择结果 |
| `v2_peer_bufs` | 卡 0 | 发送端 | 长度 7 的地址表，记录 7 个 `ll_buf` 的 GPU 指针 |
| `v2_partial_buf` | 卡 0 | 发送端 | 汇聚各卡返回的部分注意力结果（仅 0 卡需要） |

`peer_bufs` 本质是「0 卡手里的通讯录」：里面存着另外 7 张卡的门牌号（显存地址），0 卡每次出牌（P2P write）就照着通讯录发。

#### 4.2.2 核心流程

V2 P2P 的建立分两阶段：

```
阶段 1（线程内，并行）：各卡创建自己的接收/发送缓冲区
  · 卡 0 线程：创建 v2_peer_bufs（int64，长度 7）+ v2_partial_buf
              把 v2_peer_bufs 记入 self._v2_p2p[0]
  · 卡 1..7 线程：各自创建 v2_ll_buf（int32）
              把 v2_ll_buf 记入 self._v2_p2p[i]
         ↓ 8 线程 join（全部完成）
阶段 2（主线程串行）：地址回填
  · 读出卡 1..7 的 v2_ll_buf.data_ptr()（GPU 地址）
  · 打包成长度 7 的 int64 张量 peer_bufs_cpu
  · peer_bufs_cpu.copy_ 进 卡 0 的 v2_peer_bufs
         ↓
阶段 3：prepare_money（见 4.3）
```

`ll_buf` 的大小由 `ModelArgs` 决定，见 dsa.py 创建处。

#### 4.2.3 源码精读

**(a) 缓冲区在 Dsa 构造时创建**

[tilert/models/deepseek_v3_2/modules/dsa.py:39-L52](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L39-L52) `Dsa.__init__` 按卡号分支：卡 0 创建 `v2_peer_bufs`（长度 `n_peers=7` 的 int64 张量，占位用）与 `v2_partial_buf`；卡 1..7 创建 `v2_ll_buf`，大小为 `max_seq_len * topk * 2`（int32），其中 `max_seq_len = num_mtp + 1`、`topk = index_topk = 2048`。

> 注意 `v2_ll_buf` 的元素个数 = `(num_mtp+1) * index_topk * 2`。这个大小承接 u2-l2：`index_topk` 钉死注意力选择开销约 2k，与序列长度解耦。

**(b) 线程内登记缓冲区引用**

[tilert/models/deepseek_v3_2/modules/end2end.py:394-L401](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L394-L401) 每个线程把本卡的缓冲区对象（注意是对象引用，不是地址）登记进共享字典 `self._v2_p2p`：卡 0 存 `peer_bufs`，其余卡存 `ll_buf`。这一步只是「留个把手」，主线程稍后要用。

**(c) join 后的地址回填（核心）**

[tilert/models/deepseek_v3_2/modules/end2end.py:491-L501](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L491-L501) 这是 V2 P2P 的关键代码段：遍历卡 1..7，用 `self._v2_p2p[dev_id]["ll_buf"].data_ptr()` 取出每个 `ll_buf` 在其 GPU 上的设备地址，打包进一个长度 7 的 int64 CPU 张量 `peer_bufs_cpu`，再 `copy_` 进卡 0 的 `v2_peer_bufs`。日志会打印这 7 个地址的十六进制值。

`data_ptr()` 返回的是张量数据区的裸 GPU 指针（`uintptr_t`），这正是 CUDA P2P write 需要的目标地址。卡 0 的后端算子拿到 `peer_bufs` 后，就能用 CUDA 的 peer 访问 API 把稀疏选择结果直接写到这些地址。

#### 4.2.4 代码实践

**实践目标**：解释「为什么 V2 P2P 交换必须排在 8 线程 join 之后、`prepare_money` 之前」。

**操作步骤**：

1. 读 [tilert/models/deepseek_v3_2/modules/end2end.py:472-L524](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L472-L524)，注意三段代码的顺序：起线程 → `join` → V2 P2P 回填（491-501）→ `prepare_money`（503-524）。
2. 回答两个子问题（见下方「需要观察的现象」与答案）。
3. 对照 dsa.py 第 40 行与第 51 行，确认卡 0 的 `v2_peer_bufs` 在 `Dsa.__init__` 时被初始化为**全零**——也就是说，在回填之前，`peer_bufs` 里的 7 个地址都是 0（无效地址）。

**需要观察的现象 / 思考题**：

- **子问题 A（为什么 join 之后）**：如果把地址回填的循环放进某个线程内部（比如卡 0 的 `__load_weights` 里），会发生什么？
- **子问题 B（为什么 prepare_money 之前）**：如果交换顺序，先 `prepare_money` 再回填 `peer_bufs`，后端捕获的 CUDA Graph 里 0 卡的发送地址是什么？

**预期结果 / 参考答案**：

- **A**：`ll_buf` 在卡 1..7 各自的 `Dsa.__init__` 里才创建。卡 0 的线程若想读它们的 `data_ptr()`，必须等那 7 个线程都执行到 `__init__` 之后——但线程调度不保证顺序，卡 0 可能先跑完，此时 `self._v2_p2p[1..7]` 还没有 `ll_buf`。把回填放到 `join` 之后（全部线程必定已建好缓冲区）是最简单正确的串行点，且不影响 8 线程加载的并行度。
- **B**：`prepare_money` 会把张量交给 C++ 并捕获 CUDA Graph（见 4.3）。后端 MLA 稀疏选择算子在卡 0 上需要 `peer_bufs` 里的地址来发起 P2P write。若先捕获图，此时 `peer_bufs` 还是全零，图里固化了无效地址，运行时 P2P 写会写到 0x0 导致段错误。所以地址必须在图捕获之前就位。

> 待本地验证：P2P 写的实际行为需要多卡 RDMA/P2P 环境验证。这里只能从源码与 CUDA 语义推断。

#### 4.2.5 小练习与答案

**练习 1**：`peer_bufs_cpu` 是一个 CPU 张量，为什么最后要 `copy_` 进卡 0 的 `v2_peer_bufs`（GPU 张量）？

**参考答案**：因为后端算子读的是卡 0 GPU 上的 `v2_peer_bufs`。`data_ptr()` 取出的虽然是设备地址，但这些地址值本身先被收集到一个 CPU 张量里做组装，最终必须搬到卡 0 的 GPU 上供 C++ 算子读取。`copy_` 完成的是「CPU → 卡 0 GPU」的搬运。

**练习 2**：为什么只有卡 0 需要 `v2_partial_buf`，其余 7 张卡不需要？

**参考答案**：V2 通信模型里，卡 0 是稀疏选择的「广播源」与部分结果的「汇聚点」。各卡算完自己负责的注意力头后，部分结果需要汇聚到卡 0 做最终规约，所以卡 0 需要 `partial_buf` 接收这些部分结果；其余卡只接收 0 卡的稀疏选择（`ll_buf`），不需要汇聚缓冲。

---

### 4.3 prepare_money：把 Python 张量交给后端的绑定契约

#### 4.3.1 概念说明

经过 4.1（得到四元组）与 4.2（建好 P2P 通讯录）后，所有张量已经躺在各卡 GPU 上，但 C++ 后端还不知道它们的存在。`prepare_money` 就是这最后一步「交接仪式」：把每卡的四个扁平张量列表（`params / temp_vars / caches / profile_logs`）连同 `forward_max_seq_len` 一起交给后端，后端据此：

1. 记录这些张量的指针与形状；
2. 捕获 CUDA Graph（把整个 DSA 解码流程预编译成一张静态图，后续每步 `dsa_show_hands` 直接 replay，消除 kernel launch 开销——这是超低延迟的关键）。

之所以叫 `prepare_money`（备筹码）：牌局开始前，先把筹码（张量）在桌上摆好，之后每步 `show_hands`（出牌）就不再搬运筹码，只快速推演。

#### 4.3.2 核心流程

```
for device_id in 0..7:
    with torch.cuda.device(device_id):
        intermediates, caches, params, profile_logs = multi_devices_results[device_id]
        dsa_show_hands_prepare_money(params, intermediates, caches, profile_logs,
                                     forward_max_seq_len=4, with_mtp, is_glm5)
        # MTP 模式下，额外用「不含 MTP 的子集」再 prepare 一次（with_mtp=False）
        if with_mtp:
            dsa_show_hands_prepare_money(params[:base], intermediates,
                                         caches[:base], profile_logs,
                                         forward_max_seq_len=4, False, is_glm5)
```

注意四个张量列表在调用时的位置：`prepare_money(params, temp_vars, cache_vars, profile_logs, ...)`——`temp_vars` 就是四元组里的 `intermediates`，名字不同但同一个东西。

#### 4.3.3 源码精读

**(a) `prepare_money` 入口：按模式拼算子名**

[tilert/models/deepseek_v3_2/modules/end2end.py:79-L96](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L79-L96) 用 `getattr(torch.ops.tilert, func_name)` 按模式动态选择后端算子，函数名为 `dsa[_mtp_e2e]_show_hands_prepare_money[_glm5]`。注意 MTP 与非 MTP 的参数个数不同：MTP 版只传 4 个张量（不带 `forward_max_seq_len`），非 MTP 版传 5 个。

**(b) 主循环：逐卡绑定**

[tilert/models/deepseek_v3_2/modules/end2end.py:503-L524](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L503-L524) 对 8 张卡逐卡调 `prepare_money`。关键点：

- `with torch.cuda.device(device_id)`：确保绑定发生在对应卡的 CUDA 流上。
- 四元组解包顺序 `(intermediates, caches, params, profile_logs)`，但传参顺序是 `(params, intermediates, caches, profile_logs, ...)`，注意二者不一致。
- MTP 模式下会调**两次**：第一次用全量（`with_mtp=True`，绑定主模型+MTP 的完整图），第二次用 `params[:_base_params_count]` 与 `caches[:_base_caches_count]`（`with_mtp=False`，只绑定主模型部分）。这是因为 MTP 模式同时维护「主模型解码图」与「主模型+MTP 端到端图」两张图，分别用不同子集的张量捕获。

> `_base_params_count` / `_base_caches_count` 在 `__load_weights` 第 432-433 行记录，等于不含 MTP 时的列表长度。

**(c) 出牌：`dsa_show_hands` 与 `forward`**

绑定完成后，每步解码只需调一次 `dsa_show_hands`。[tilert/models/deepseek_v3_2/modules/end2end.py:99-L104](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L99-L104) 按模式拼名，调用后端算子，入参只有一个 `token_id`（搬到 CPU）。[tilert/models/deepseek_v3_2/modules/end2end.py:551-L558](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L551-L558) 是 Python 侧 `forward` 封装：调 `dsa_show_hands` 后返回 8 卡的结果引用（结果已经写进绑定的 `intermediates` 里，这里只是取引用）。

**(d) 收摊：`go_home` 与采样配置重捕**

[tilert/models/deepseek_v3_2/modules/end2end.py:579-L584](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L579-L584) `cleanup` 调 `go_home` 释放后端资源（MTP 模式调两次）；`__del__` 兜底也会调。

采样参数若运行时改变，必须先 `go_home` 释放旧图、改完 `SAMPLING_CONFIG` 槽、再重跑 `prepare_money` 重捕图——见 [tilert/models/deepseek_v3_2/modules/end2end.py:253-L311](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L253-L311)（u3-l4 会详讲）。这里只需记住：`prepare_money` 捕获的图把采样参数固化了，所以换参数=换图。

#### 4.3.4 代码实践

**实践目标**：理解 `prepare_money` 的「两遍调用」与四元组的解包/传参差异。

**操作步骤**：

1. 读 [tilert/models/deepseek_v3_2/modules/end2end.py:503-L524](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L503-L524)，对照 `dsa_show_hands_prepare_money` 签名（第 79-96 行）。
2. 写一小段**伪代码**（不是项目原有代码），模拟 MTP 模式下两遍调用的区别：

```python
# 示例代码：仅为说明两遍 prepare_money 的差异，非项目原有代码
def bind(device_id, params, intermediates, caches, profile_logs, base_p, base_c):
    # 第一遍：完整图（主模型 + MTP）
    prepare_money(params, intermediates, caches, profile_logs, seq_len=4, with_mtp=True)
    # 第二遍：仅主模型图（切片，with_mtp=False）
    prepare_money(params[:base_p], intermediates, caches[:base_c], profile_logs,
                  seq_len=4, with_mtp=False)
```

3. 解释为什么第二遍要把 `params` 和 `caches` 切片，而 `intermediates` 不切片。

**需要观察的现象**：

- `intermediates`（temp_vars）在两遍调用里都是**全量**，因为主模型图和 MTP 图共用同一组激活槽（如 `LOGITS_OUT`、`SAMPLING_CONFIG` 等）。
- `params[:base_p]` 排除了 MTP 模块的权重，因为主模型图不需要 MTP 权重。

**预期结果**：MTP 模式下后端持有两张 CUDA Graph——一张跑「主模型解码」（用于校验接受 draft token），一张跑「主模型 + MTP 端到端」（用于产出 draft token）。非 MTP 模式只有一张图。

> 待本地验证：两张图的实际行为需在后端 `.so` 层面观察，本讲只能从 Python 侧的调用约定推断。

#### 4.3.5 小练习与答案

**练习 1**：`forward` 方法（第 551-558 行）调用 `dsa_show_hands(token_id.cpu(), ...)` 后直接返回 8 卡的 `multi_devices_results` 引用。它为什么不需要「等待计算完成」？

**参考答案**：`dsa_show_hands` 在后端是同步触发 CUDA Graph replay 的算子（默认在默认流上），Python 侧调用返回时图已 launch。结果直接写进之前 `prepare_money` 绑定的 `intermediates` 张量里，所以 `forward` 只需返回这些张量的引用，调用方读张量时自然会看到最新结果（若跨流则由 CUDA 同步保证）。

**练习 2**：`update_sampling_config` 在参数变化时为什么要先 `go_home` 再 `prepare_money`，而不是直接改 `SAMPLING_CONFIG` 槽？

**参考答案**：因为采样参数已经被 CUDA Graph 固化进去了。直接改 `SAMPLING_CONFIG` 张量的值，图 replay 时读到的可能是旧值（取决于图捕获时是按指针还是按值读）。`go_home` 释放旧图后，改完槽位再 `prepare_money` 重新捕获一张包含新参数的图，才能保证采样行为正确更新。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画出 `ShowHandsDSALayer` 从 `from_pretrained` 到 `prepare_money` 的完整时序图。

**操作步骤**：

1. 假设 `DSAv32Generator` 已构造好（`decode_layer` 已是 `ShowHandsDSALayer` 实例，但尚未加载权重）。从 `generator.from_pretrained()` 开始（[generator.py:103-L105](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L103-L105)）。
2. 追踪到 `decode_layer.from_pretrained(model_path)`（[end2end.py:526-L530](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L526-L530)），再进 `_init_weights`。
3. 画一张时序图，包含以下泳道（横轴为时间）：
   - **主线程**：起 8 线程 → join → V2 P2P 回填 → 8 卡 `prepare_money`
   - **8 个加载线程**：并行执行 `load_device_weights` → 构造 `Dsa` → `init_tilert_weights` → 压扁四元组
4. 在图上标注三个关键时序约束，并写一句解释：
   - 「`Dsa` 构造 → `v2_ll_buf` 创建」必须早于「地址回填」；
   - 「8 线程 join」必须早于「地址回填」；
   - 「地址回填」必须早于「`prepare_money`」。
5. 最后回答：如果某张卡（比如卡 5）的加载线程抛异常，整个流程会发生什么？（提示：看 [end2end.py:487-L489](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L487-L489) 的异常收口。）

**预期结果**：一张清晰的时序图，能看出「加载」与「地址回填」是串行的两个阶段（前者并行、后者串行），而 `prepare_money` 必须排在最后。卡 5 异常时，`exceptions[5]` 被捕获，`join` 后主线程抛 `RuntimeError`，`prepare_money` 不会执行，避免在不完整状态下捕获图。

> 待本地验证：完整时序需要真实 8 卡环境运行并配合 `logger.info` 日志验证（源码在第 470、498 行有计时与地址日志）。

## 6. 本讲小结

- `ShowHandsDSALayer` 是 Generator 的「牌桌管家」：构造廉价（只记配置），真正的 8 卡加载发生在 `from_pretrained` → `_init_weights`。
- **多线程分卡加载**利用了离线转换产生的 `*_dev_{id}` 键名分区：8 个线程各读各的分片、各装配各的 `Dsa` 树、各压扁出 `(intermediates, caches, params, profile_logs)` 四元组，天然并行无冲突。
- **V2 P2P** 用 CUDA peer-to-peer 直写代替 allreduce：卡 1..7 各建 `ll_buf` 接收缓冲，主线程在 `join` 之后把 7 个 `ll_buf` 的 GPU 地址回填进卡 0 的 `peer_bufs`「通讯录」。
- 时序三约束：`ll_buf` 创建 → join → 地址回填 → `prepare_money`，任何一步前置都会导致地址无效或图固化错误地址。
- **`prepare_money`** 是把 Python 张量交给 C++ 后端并捕获 CUDA Graph 的「交接仪式」；MTP 模式会调用两遍（完整图 + 主模型子图）。
- 采样参数固化在图里，运行时改参数需 `go_home` 释放旧图、改 `SAMPLING_CONFIG` 槽、再 `prepare_money` 重捕。

## 7. 下一步学习建议

- **u2-l4（DSA 层组装）**：本讲把 `Dsa` 当作「已装配好的容器」直接用，下一讲拆开 `Dsa` 内部 `register_op` 的 `prefix/suffix` 拼接与 dense/MoE 分界，弄清四元组里的 `params` 列表是怎么一层层组装出来的。
- **u2-l5（三层张量契约）**：本讲的 `intermediates` 就是那里讲的 `temp_vars`；建议接着精读 `get_temp_vars` 与 `Idx` 枚举，理解 56 个激活槽各自的 dtype/shape 来源。
- **u3-l2（生成主循环）**：理解 `prepare_money` 之后，再看 `dsa_show_hands` 如何在主循环里被反复调用、结果如何写回 `intermediates`。
- **扩展阅读**：`from_pretrained_with_cache` 与 `_extract_ffn_ops` / `_get_moe_weight_keys`（[end2end.py:37-L76](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L37-L76)）实现了一种「复用已加载 MoE/MLP 算子、只重载其余权重」的优化，适合在掌握本讲后作为进阶阅读。
