# 快照：保存与加载训练结果

## 1. 本讲目标

instant-ngp 训练一个模型通常只需几秒到几十秒，但「几秒」也想存下来——因为 NeRF/SDF 训练完要拿去渲染视频、出截图、提网格，重新训练一遍既浪费又无法复现完全相同的权重。**快照（snapshot）**就是 instant-ngp 把「当前训练好的模型」连同「重建它所需的全部信息」一起写进一个文件的机制。

学完本讲，你应当能够：

- 说清楚 `save_snapshot` 到底把哪些东西写进了文件（权重、配置、训练步数、相机、密度网格……）。
- 区分 `.ingp`（zlib 压缩的 msgpack）与 `.msgpack`（裸 msgpack）两种磁盘格式，以及为什么 GUI 里的「Compress」复选框只在 `.ingp` 时可用。
- 跟踪 `load_network_config` 对 `.ingp` / `.msgpack` / `.json` 三种后缀的不同解析路径，并解释为什么快照里**必须同时保存完整的网络配置**。
- 理解 `include_optimizer_state` 的作用：带上优化器动量能「继续训练」，不带就只能「推理/渲染」。
- 用 `scripts/run.py` 走一遍「保存 → 加载 → 继续训练」的完整流程。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在前置讲义里已建立，这里只做最小回顾）：

- **网络配置（network config）**：一份 JSON，描述网络用什么编码、多大哈希表、几层 MLP、什么损失与优化器。它只描述「架构」，不含权重（见 u2-l4）。
- **`reset_network()`**：消费 `m_network_config`，借助 tiny-cuda-nn 的工厂函数按依赖顺序构造 Loss/Optimizer/Encoding/Network/Trainer 五大对象，并把训练步数归零（见 u3-l1）。
- **`m_network_config` 与 `m_network_config_path`**：Testbed 持有的两个成员。前者是**已合并 parent 的完整配置**（内存里的 JSON 对象），后者是它在磁盘上的路径。
- **parent 继承与 `merge_parent_network_config`**：子配置写 `"parent"` 指向父配置，加载时用深度合并把父摊平进子（见 u2-l4）。这是理解「快照为什么不再 merge」的关键。
- **`m_trainer`（Trainer）**：来自外部依赖 tiny-cuda-nn 的「总指挥」对象，编排前向/损失/反向/更新，掌管全部可训练参数，并对外提供 `serialize()` / `deserialize()`。

一句话定位：**网络配置 = 图纸，快照 = 图纸 + 已造好的实物**。本讲讲的就是「实物如何被打包、运输、再组装」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | 快照的全部核心逻辑：`save_snapshot`、三个 `load_snapshot` 重载、`load_network_config`、`reload_network_from_file`、`load_file` 的分发、GUI 的 Snapshot 面板。 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | 声明四个快照函数，以及两个控制开关 `m_include_optimizer_state_in_snapshot`、`m_compress_snapshot`。 |
| [src/python_api.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu) | pybind11 绑定，把 `save_snapshot` / `load_snapshot` 暴露给 Python（pyngp）。 |
| [scripts/run.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py) | Python 自动化入口：`--load_snapshot` / `--save_snapshot` 两条命令行选项及其用法。 |
| [scripts/scenes.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/scenes.py) | `default_snapshot_filename`：把内置示例场景名翻译成默认的 `.ingp` 文件名。 |

> 说明：真正把权重序列化为字节的 `Trainer::serialize()` / `Trainer::deserialize()` 实现位于外部依赖 **tiny-cuda-nn**（`dependencies/tiny-cuda-nn`），不在本仓库。本讲只在调用点（testbed.cu）引用它，描述其行为，不深入其实现。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** 快照保存了什么：`save_snapshot` 的序列化内容
- **4.2** 两种磁盘格式：`.ingp` 与 `.msgpack`，以及压缩选项
- **4.3** 三分支解析路径：`load_network_config` 如何区分 `.ingp` / `.msgpack` / `.json`
- **4.4** 加载还原与优化器状态：`load_snapshot` + `reset_network(false)` + `include_optimizer_state`

### 4.1 快照保存了什么：save_snapshot 的序列化内容

#### 4.1.1 概念说明

很多人以为「快照 = 把网络权重 dump 到磁盘」。这只对了一半。instant-ngp 的快照是一个**自包含的训练现场**：你拿到一个 `.ingp` 文件，即便脱离原来的 `configs/` 目录、脱离原始数据集元数据，也能完整还原出训练那一刻的网络、相机、密度网格和训练进度。

它由两部分粘在一个 JSON 对象里：

1. **完整的网络配置**（`m_network_config` 的全部内容：encoding / network / optimizer / loss ……，且 parent 已摊平）；
2. **一个名为 `"snapshot"` 的子对象**，装着运行时状态：序列化权重、版本号、模式、训练步数、相机参数、（NeRF 专有的）密度网格与相机优化偏移等。

之所以要把配置也塞进去，是因为**权重只有配合「同样架构」的网络才有意义**——一个为 `n_levels=16` 哈希编码训练出的权重张量，塞进 `n_levels=8` 的网络就是一堆乱码。加载时必须先用配置重建出拓扑完全一致的网络，再把权重 `deserialize` 回去。

#### 4.1.2 核心流程

`save_snapshot` 的工作可以概括为「填一个 JSON，再选一种方式写盘」：

```
save_snapshot(path, include_optimizer_state, compress):
  1. m_network_config["snapshot"] = m_trainer->serialize(include_optimizer_state)
       # 让 Trainer 把权重（可选含优化器动量）序列化进 JSON
  2. 给 snapshot 补元数据：
       version  = SNAPSHOT_FORMAT_VERSION  # 版本号，防旧格式
       mode     = to_string(m_testbed_mode) # Nerf/Sdf/Image/Volume
       training_step, loss, aabb, bounding_radius, 相机, ……（通用状态）
       若 NeRF：density_grid_size、density_grid_binary、
               aabb_scale、cam_pos_offset、cam_rot_offset、extra_dims_opt、
               rays_per_batch、measured_batch_size、dataset 元数据
  3. m_network_config_path = path
  4. 打开 ofstream（二进制）：
       若扩展名是 .ingp → zstr::ostream 套一层 zlib，再 json::to_msgpack
       否则            → 直接 json::to_msgpack（无压缩）
  5. tlog::success "Saved snapshot"
```

注意第 1 步：快照是直接写进**已有的 `m_network_config`**（内存里那份完整配置）的 `"snapshot"` 键。这就是「配置 + 实物」装进同一个 JSON 的实现方式。

#### 4.1.3 源码精读

首先是版本号常量与函数签名，位于 [src/testbed.cu:5285-5288](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5285-L5288)——注释明确说明「修改快照格式时要把这个版本号加 1」，它是 `load_snapshot` 拒绝旧格式的依据：

```cpp
// Increment this number when making a change to the snapshot format
static const size_t SNAPSHOT_FORMAT_VERSION = 1;

void Testbed::save_snapshot(const fs::path& path, bool include_optimizer_state, bool compress) {
	m_network_config["snapshot"] = m_trainer->serialize(include_optimizer_state);
```

[src/testbed.cu:5289](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5289) 这一行是整个快照的核心：`m_trainer->serialize(include_optimizer_state)` 让 tiny-cuda-nn 的 Trainer 把全部可训练参数（哈希表 + MLP 权重，可选地含 Adam 的一阶/二阶动量）打包成可写入 JSON 的二进制字段。注意它是写到 `m_network_config["snapshot"]`——也就是说权重和配置**在同一个 JSON 对象树里**。

接着补版本号和模式，[src/testbed.cu:5291-5293](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5291-L5293)：

```cpp
auto& snapshot = m_network_config["snapshot"];
snapshot["version"] = SNAPSHOT_FORMAT_VERSION;
snapshot["mode"] = to_string(m_testbed_mode);
```

NeRF 模式有额外的高价值状态——密度网格（用来跳过空区域，见 u4-l5）。它先被 GPU 核从 float 转成 fp16 以省一半空间，再写进 `density_grid_binary`，并保存相机自标定的偏移量，见 [src/testbed.cu:5295-5312](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5295-L5312)：

```cpp
if (m_testbed_mode == ETestbedMode::Nerf) {
    snapshot["density_grid_size"] = NERF_GRIDSIZE();
    GPUMemory<__half> density_grid_fp16(m_nerf.density_grid.size());
    parallel_for_gpu(... density_grid_fp16[i] = (__half)density_grid[i]; ...);
    snapshot["density_grid_binary"] = density_grid_fp16;
    snapshot["nerf"]["aabb_scale"] = m_nerf.training.dataset.aabb_scale;
    snapshot["nerf"]["cam_pos_offset"] = m_nerf.training.cam_pos_offset;
    snapshot["nerf"]["cam_rot_offset"] = m_nerf.training.cam_rot_offset;
    snapshot["nerf"]["extra_dims_opt"] = m_nerf.training.extra_dims_opt;
}
```

然后是一批**与具体模式无关的通用状态**——训练步数、损失、包围盒、相机内外参、曝光、背景色等，[src/testbed.cu:5314-5341](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5314-L5341)。这些是「渲染现场」：加载后相机停在保存时的位置、训练步数接着原来的数字继续走。

最后是落盘，根据扩展名决定是否 zlib 压缩，[src/testbed.cu:5344-5352](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5344-L5352)：

```cpp
m_network_config_path = path;
std::ofstream f{native_string(m_network_config_path), std::ios::out | std::ios::binary};
if (equals_case_insensitive(m_network_config_path.extension(), "ingp")) {
    // zstr::ofstream applies zlib compression.
    zstr::ostream zf{f, zstr::default_buff_size, compress ? Z_DEFAULT_COMPRESSION : Z_NO_COMPRESSION};
    json::to_msgpack(m_network_config, zf);
} else {
    json::to_msgpack(m_network_config, f);
}
```

无论哪条分支，内存模型都是「JSON → msgpack 二进制」。区别只在于 `.ingp` 多套了一层 `zstr`（zlib）流；`compress` 参数进一步控制这一层是 `Z_DEFAULT_COMPRESSION` 还是 `Z_NO_COMPRESSION`（即「存成 .ingp 但不压缩」）。

#### 4.1.4 代码实践

**实践目标**：从源码确认「快照里同时存了配置和权重」这一论断。

**操作步骤**：

1. 打开 [src/testbed.cu:5288-5355](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5288-L5355) 的 `save_snapshot` 全文。
2. 找到第 5289 行，确认它写的是 `m_network_config["snapshot"]`，即**在已有配置对象上挂一个 snapshot 子键**，而不是另起一个只含权重的文件。
3. 对照 [include/neural-graphics-primitives/testbed.h:625-626](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L625-L626) 的两个开关 `m_include_optimizer_state_in_snapshot` 与 `m_compress_snapshot`，理解它们分别对应 `save_snapshot` 的第 2、第 3 个参数。

**需要观察的现象 / 预期结果**：

- `save_snapshot` 全程没有「重新读取 configs/ 下的 JSON」的步骤——因为 `m_network_config` 在加载配置时就已经把 parent 摊平好了，保存时直接整体写出。
- 权重来自 `m_trainer->serialize(...)`，配置来自 `m_network_config`，二者合流到同一个 JSON 再写盘。这就是「快照自包含」的根因。

> 待本地验证：若有编译好的 `instant-ngp`，训练 fox 几秒后存一个 `base.ingp`，用 `python -c "import zlib,msgpack,sys; print(list(msgpack.unpackb(zlib.decompress(open('base.ingp','rb').read()),raw=False).keys()))"`（粗略）可看到顶层同时有 `encoding`/`network`/`optimizer`/`loss` 与 `snapshot` 键。

#### 4.1.5 小练习与答案

**练习 1**：如果未来某次重构把密度网格从 128³ 改成 256³，旧快照加载时会发生什么？源码依据在哪？

**参考答案**：加载侧在 [src/testbed.cu:5376-5378](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5376-L5378) 检查 `snapshot["density_grid_size"] != NERF_GRIDSIZE()` 会抛 `"Incompatible grid size."`。同时 `SNAPSHOT_FORMAT_VERSION` 也应当随之 +1，使更旧的快照在 [src/testbed.cu:5359-5361](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5359-L5361) 处直接被拒。

**练习 2**：为什么 `save_snapshot` 不需要传入「网络配置」，却能把配置写进文件？

**参考答案**：因为 Testbed 始终在内存里持有合并后的完整配置 `m_network_config`（加载配置时由 `merge_parent_network_config` 摊平 parent 得到）。保存只是把这个对象加一个 `"snapshot"` 子键后整体序列化，无需再次读盘。

### 4.2 两种磁盘格式：.ingp 与 .msgpack

#### 4.2.1 概念说明

instant-ngp 支持两种快照后缀，本质都是 **msgpack 二进制**，差别只在有没有套一层 zlib：

| 后缀 | 容器 | 是否压缩 | 推荐度 |
| --- | --- | --- | --- |
| `.ingp` | msgpack，外层 zlib | 默认压缩（可关） | **推荐**，体积小、是项目「品牌」格式 |
| `.msgpack` | 裸 msgpack | 否 | 兼容/调试用，体积约为 `.ingp` 的数倍 |

权重张量（尤其哈希表）对压缩很友好——大片浮点数经 zlib 后通常能压到原始大小的几分之一，所以 `.ingp` 是默认且推荐的快照格式。`run.py` 的帮助文本也明确写着 `recommended extension: .ingp/.msgpack`（见 [scripts/run.py:36-37](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L36-L37)）。

#### 4.2.2 核心流程

保存侧的格式选择已在上节 [src/testbed.cu:5344-5352](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5344-L5352) 看到：扩展名是 `ingp` 才走 zlib，否则裸 msgpack。注意一个细节——**`compress` 开关只对 `.ingp` 有效**：`.msgpack` 路径根本没有套 zlib，无从压缩。这一点在 GUI 里有对应的禁用逻辑。

GUI 的 Snapshot 面板用一个 `can_compress` 标志把「Compress」复选框在非 `.ingp` 时灰掉，见 [src/testbed.cu:1914-1922](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1914-L1922)：

```cpp
bool can_compress = ends_with_case_insensitive(m_imgui.snapshot_path, ".ingp");
ImGui::BeginDisabled(!can_compress);
if (!can_compress) {
    m_compress_snapshot = false;
}
ImGui::Checkbox("Compress", &m_compress_snapshot);
ImGui::EndDisabled();
```

读取侧的对称逻辑在 `load_network_config` 里，下节详述。

#### 4.2.3 源码精读

GUI 面板整体（Save/Load 按钮、`w/ optimizer state` 复选框、文件路径输入框、Compress 复选框）位于 [src/testbed.cu:1882-1923](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1882-L1923)。Save 按钮直接把两个成员变量喂给 `save_snapshot`：

```cpp
save_snapshot(m_imgui.snapshot_path, m_include_optimizer_state_in_snapshot, m_compress_snapshot);
```

（[src/testbed.cu:1889](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1889)）

#### 4.2.4 代码实践

**实践目标**：直观感受 `.ingp` 与 `.msgpack` 的体积差异。

**操作步骤**：

1. 用 GUI 或 pyngp 把同一个训练好的 fox 模型分别存成 `fox.ingp`（Compress 勾上）与 `fox.msgpack`。
2. 对比两个文件大小。

**需要观察的现象 / 预期结果**：`.ingp` 通常明显小于 `.msgpack`（常常只有后者的一半甚至更少）。

> 待本地验证：具体压缩比取决于场景（密度网格、哈希表填充率），以你机器上的实测为准。

#### 4.2.5 小练习与答案

**练习 1**：用户在 GUI 把文件名填成 `out.msgpack`，为什么「Compress」复选框会变灰？

**参考答案**：[src/testbed.cu:1914](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1914) 用 `ends_with_case_insensitive(..., ".ingp")` 算 `can_compress`；`.msgpack` 时为 false，复选框被 `BeginDisabled` 灰掉，并把 `m_compress_snapshot` 强制置 false。因为 `save_snapshot` 里只有 `.ingp` 分支才套 zlib，对 `.msgpack` 谈压缩没有意义。

### 4.3 三分支解析路径：load_network_config

这是本讲对应实践任务的核心。`load_network_config` 是「把磁盘文件读成内存 JSON 对象」的统一入口，它按后缀走三条完全不同的路径。

#### 4.3.1 概念说明

`load_network_config` 有两个重载：一个吃 `std::istream`（配合 `is_compressed` 标志，给流式 API 用），一个吃 `fs::path`（按扩展名自动判别）。后者是主力，它区分三类文件：

| 后缀 | 判定 | 解析方式 | 是否 merge parent |
| --- | --- | --- | --- |
| `.ingp` | 快照 | zlib 解压 → `from_msgpack` | **否**（parent 已摊平） |
| `.msgpack` | 快照 | `from_msgpack` | **否** |
| `.json` | 配置 | `json::parse` → `merge_parent_network_config` | **是** |

关键差异在最后一列：**只有 `.json` 配置会做 parent 合并，快照不做**。原因正是 4.1 节埋下的伏笔——保存快照时写进去的 `m_network_config` 是**已经合并完 parent 的完整配置**，所以加载时不需要、也不能再去读原始的 parent 链。代码注释直白地点出这一点：`// we assume parent pointers are already resolved in snapshots.`

这同时回答了实践任务的核心问题——**为什么快照里要同时保存 network 配置**：

1. 还原网络必须先用配置 `reset_network()` 重建出**拓扑一致**的网络，才能 `deserialize` 权重；光有权重没法凭空构造网络。
2. 因为快照**不做 parent 合并**，配置必须以「已摊平」的完整形态躺在文件里，否则脱离原 `configs/` 目录后就缺胳膊少腿。所以快照把完整配置和权重打包在一起，做到**自包含、可移植**。

#### 4.3.2 核心流程

```
load_network_config(path):
  is_snapshot = (ext == "msgpack") || (ext == "ingp")
  若 is_snapshot:
      打开 ifstream（二进制）
      若 ext == "ingp":  zstr::istream 套 zlib → from_msgpack   # 解压再反序列化
      否则 (msgpack):    from_msgpack                            # 直接反序列化
      # 不调用 merge_parent_network_config
  否则若 ext == "json":
      json::parse(f)（允许抛异常、忽略注释）
      result = merge_parent_network_config(result, path)          # 递归摊平 parent
  返回 json 对象
```

`merge_parent_network_config` 本身是个简洁的递归函数：若子配置有 `"parent"` 键，就读父文件、先递归摊平父、再用 `merge_patch` 把子叠上去，见 [src/testbed.cu:86-97](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L86-L97)：

```cpp
json merge_parent_network_config(const json& child, const fs::path& child_path) {
    if (!child.contains("parent")) {
        return child;
    }
    fs::path parent_path = child_path.parent_path() / std::string(child["parent"]);
    ...
    json parent = json::parse(f, nullptr, true, true);
    parent = merge_parent_network_config(parent, parent_path); // 递归
    parent.merge_patch(child);                                  // 子覆盖父
    return parent;
}
```

快照路径**完全不进入这个函数**，因此也就不依赖磁盘上的 parent 文件存在。

#### 4.3.3 源码精读

istream 版重载，按 `is_compressed` 标志决定是否套 zlib，[src/testbed.cu:272-278](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L272-L278)：

```cpp
json Testbed::load_network_config(std::istream& stream, bool is_compressed) {
    if (is_compressed) {
        zstr::istream zstream{stream};
        return json::from_msgpack(zstream);
    }
    return json::from_msgpack(stream);
}
```

path 版重载（主力），三分支判别，[src/testbed.cu:280-309](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L280-L309)：

```cpp
json Testbed::load_network_config(const fs::path& network_config_path) {
    bool is_snapshot = equals_case_insensitive(network_config_path.extension(), "msgpack") ||
        equals_case_insensitive(network_config_path.extension(), "ingp");
    ...
    json result;
    if (is_snapshot) {
        std::ifstream f{native_string(network_config_path), std::ios::in | std::ios::binary};
        if (equals_case_insensitive(network_config_path.extension(), "ingp")) {
            // zstr::ifstream applies zlib compression.
            zstr::istream zf{f};
            result = json::from_msgpack(zf);
        } else {
            result = json::from_msgpack(f);
        }
        // we assume parent pointers are already resolved in snapshots.
    } else if (equals_case_insensitive(network_config_path.extension(), "json")) {
        std::ifstream f{native_string(network_config_path)};
        result = json::parse(f, nullptr, true, true);
        result = merge_parent_network_config(result, network_config_path);
    }
    return result;
}
```

注意 [src/testbed.cu:301](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L301) 那行注释——它就是「快照不 merge」的合同。

#### 4.3.4 代码实践

**实践目标**：跟踪 `.ingp` 与 `.json` 两种文件在 `load_network_config` 里的不同命运，并据此回答「为什么快照要同时保存 network 配置」。

**操作步骤**：

1. 阅读 [src/testbed.cu:280-309](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L280-L309) 的 `load_network_config(path)`。
2. 画出两条路径：
   - `.ingp`：`zstr`（zlib 解压）→ `from_msgpack` → 返回（含权重 + 已摊平配置，**不 merge**）。
   - `.json`：`json::parse` → `merge_parent_network_config`（读 parent 文件、递归合并）→ 返回（**仅配置，无权重**）。
3. 再看 [src/testbed.cu:341-343](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L341-L343)：`reload_network_from_file` 在加载**非快照**（`.json` 配置）后会调用 `reset_network()` 重新从头训练；而快照路径不在这里 reset——它走的是 `load_snapshot`，下节会看到它在 `reset_network(false)` 后立刻 `deserialize` 权重。

**需要观察的现象 / 预期结果**：

- `.json` 配置加载后会触发 `reset_network()`（训练步数归零，从头训），因为它只有图纸没有实物。
- `.ingp` 快照加载后训练步数**接着保存时的数字继续**，因为权重被 `deserialize` 还原了。
- 据此回答核心问题：快照里必须保存完整 network 配置，①是为了 `reset_network` 重建同拓扑网络以承接权重；②是因为快照加载不 merge parent，配置只能以摊平后的完整形态随文件携带，保证自包含可移植。

> 一个值得注意的**细节（非主路径）**：`reload_network_from_file` 在 [src/testbed.cu:330](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L330) 判断 `is_snapshot` 时**只看 `.msgpack`、不看 `.ingp`**。这意味着若你拿 `--network foo.ingp` 走 reload 路径，它会被当成普通配置而触发 `reset_network()`（从头训）。这正是为什么加载 `.ingp` 应该用专门的 `load_snapshot` / `--load_snapshot`，而不是 `--network`。

#### 4.3.5 小练习与答案

**练习 1**：把一个 `configs/nerf/hashgrid.json`（带 `"parent": "base.json"`）原样改名为 `hashgrid.ingp` 去加载，会怎样？

**参考答案**：会失败或得到错误结果。`.ingp` 走快照分支，按 msgpack 反序列化并**跳过 parent 合并**；而它其实是文本 JSON 配置且依赖 parent，既不是合法 msgpack、也没被 merge。这反向印证了「后缀决定解析路径」，文件内容必须与后缀匹配。

**练习 2**：为什么 `load_network_config` 的快照分支敢假设 parent 已摊平？

**参考答案**：因为 `save_snapshot` 写入的 `m_network_config` 是配置加载阶段经过 `merge_parent_network_config` 摊平后的完整对象（[src/testbed.cu:5289](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5289) 直接用 `m_network_config`）。保存即冻结了完整配置，加载时无需、也不应再次合并。

### 4.4 加载还原与优化器状态：load_snapshot + include_optimizer_state

#### 4.4.1 概念说明

`load_snapshot` 有三个重载，层层委托：

```
load_snapshot(path)      // 给 pyngp / GUI / CLI 用
  → load_network_config(path)   // 读盘 + 解析（4.3 节）
  → load_snapshot(json)         // 真正的还原逻辑（本节）

load_snapshot(stream, is_compressed)  // 给流式 API 用
  → load_network_config(stream, is_compressed)
  → load_snapshot(json)
```

真正干活的是 `load_snapshot(json)`。它的还原顺序非常讲究，可以用一句话概括：**先用配置重建空网络，再把权重灌回去，最后补运行时状态**。

其中最关键、也最容易被忽略的一步是 `reset_network(false)`——注意那个 `false` 参数。`reset_network` 在 u3-l1 里默认会把 `m_training_step` 归零（从头训）；这里传 `false` 表示「**不要清零训练状态**」，因为马上要用快照里的 `training_step` 把它恢复回来。这一步是「加载快照后接着训练」而不是「从头训」的根本原因。

另一个要点是 `include_optimizer_state`（优化器状态）。Adam 优化器除了参数本身，还维护着每个参数的一阶动量（m）和二阶动量（v）。这两个动量是训练「惯性」：

- **带上优化器状态**（`include_optimizer_state=true`）：保存 m/v，加载后优化器从原来的惯性继续，**可以平滑地继续训练**。
- **不带优化器状态**（默认 `false`）：只存参数权重，加载后优化器动量归零。适合「只想推理/渲染、不打算再训」的场景——文件更小，且避免陈旧动量误导后续（少量）训练。

GUI 的 `w/ optimizer state` 复选框、pyngp 的 `include_optimizer_state` 参数、`run.py` 里 `save_snapshot(args.save_snapshot, False)` 的第二个参数，控制的都是这件事。

#### 4.4.2 核心流程

`load_snapshot(json)` 的还原步骤：

```
load_snapshot(config):
  1. 版本校验：snapshot.version >= SNAPSHOT_FORMAT_VERSION，否则抛错
  2. 由 snapshot.mode 调 set_mode(...)        # 切到正确基元，清空旧状态
  3. 还原通用几何/相机状态：aabb、bounding_radius、相机矩阵、曝光……
     （NeRF：还原 density_grid、counters、必要时从快照补 dataset 元数据）
  4. m_network_config = config                 # 把完整配置装回 Testbed
  5. reset_network(false)                      # 用配置重建网络，但不清训练步数
  6. m_training_step = snapshot.training_step  # 恢复训练步数
     m_loss_scalar = snapshot.loss
  7. m_trainer->deserialize(snapshot)          # 把权重（±优化器动量）灌回网络
  8. （NeRF）若数据集一致，还原 cam_pos/rot_offset、extra_dims_opt 等优化量
  9. set_all_devices_dirty()                   # 通知多 GPU 副本刷新
```

步骤 5 与 7 的先后是铁律：**先建网，后灌权重**。配置（步骤 4 装回的 `m_network_config`）决定网络的形状与大小，`deserialize`（步骤 7）按这个形状把字节填回去——形状不对就填不进去。

#### 4.4.3 源码精读

三个重载的签名与声明在 [include/neural-graphics-primitives/testbed.h:562-565](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L562-L565)：

```cpp
void save_snapshot(const fs::path& path, bool include_optimizer_state, bool compress);
void load_snapshot(nlohmann::json config);
void load_snapshot(const fs::path& path);
void load_snapshot(std::istream& stream, bool is_compressed = true);
```

path 版与 stream 版只是「先读盘/读流，再委托」，见 [src/testbed.cu:5493-5514](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5493-L5514)。它们都先确认 JSON 里有 `"snapshot"` 键，否则报错：

```cpp
void Testbed::load_snapshot(const fs::path& path) {
    auto config = load_network_config(path);
    if (!config.contains("snapshot")) {
        throw std::runtime_error{fmt::format("File '{}' does not contain a snapshot.", path.str())};
    }
    load_snapshot(std::move(config));
    m_network_config_path = path;
}
```

真正干活的 json 版，开头做版本与模式校验，[src/testbed.cu:5357-5370](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5357-L5370)：

```cpp
void Testbed::load_snapshot(nlohmann::json config) {
    const auto& snapshot = config["snapshot"];
    if (snapshot.value("version", 0) < SNAPSHOT_FORMAT_VERSION) {
        throw std::runtime_error{"Snapshot uses an old format and can not be loaded."};
    }
    if (snapshot.contains("mode")) {
        set_mode(mode_from_string(snapshot["mode"]));
    } else if (snapshot.contains("nerf")) {
        // To be able to load old NeRF snapshots that don't specify their mode yet
        set_mode(ETestbedMode::Nerf);
    } else if (m_testbed_mode == ETestbedMode::None) {
        throw std::runtime_error{"Unknown snapshot mode. ..."};
    }
```

NeRF 分支会还原密度网格：把 fp16 转回 float、重建位域金字塔，并校验网格尺寸与级联数兼容，[src/testbed.cu:5375-5418](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5375-L5418)（其中 [src/testbed.cu:5402-5410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5402-L5410) 把 fp16 密度转回 float，[src/testbed.cu:5412-5417](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5412-L5417) 重建 bitfield 并校验级联数）。

接着是全流程最关键的几行——装回配置、`reset_network(false)`、恢复步数、灌权重，[src/testbed.cu:5461-5468](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5461-L5468)：

```cpp
m_network_config = std::move(config);

reset_network(false);

m_training_step = m_network_config["snapshot"]["training_step"];
m_loss_scalar.set(m_network_config["snapshot"]["loss"]);

m_trainer->deserialize(m_network_config["snapshot"]);
```

注意三件事：

1. `reset_network(false)` 用刚装回的 `m_network_config` 重建出拓扑一致的网络（含 encoding/network/optimizer/loss），那个 `false` 让它**不清训练状态**。
2. 紧接着把 `training_step`/`loss` 从快照恢复——所以加载后训练步数接着走，不是从 0 开始。
3. `m_trainer->deserialize(...)` 把 `serialize` 时存下的字节（权重，可选含 Adam 动量）灌回刚建好的网络。`include_optimizer_state` 的影响正是在这一步显现：存的时候带没带动量，决定这里能不能恢复优化器惯性。

最后，若 NeRF 快照来自同一数据集，还会把相机自标定的偏移（`cam_pos_offset`/`cam_rot_offset`）与每图潜码（`extra_dims_opt`）还原，[src/testbed.cu:5470-5488](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5470-L5488)，并以 `set_all_devices_dirty()` 收尾通知多 GPU 刷新（多 GPU 详见 u8-l1）。

#### 4.4.4 代码实践

**实践目标**：用 `scripts/run.py` 走完一次「保存 → 加载 → 继续训练」，并体会 `include_optimizer_state` 的取舍。

**操作步骤**：

1. **第一次训练并保存**（不带优化器状态，这是 run.py 的默认）：

   ```bash
   python scripts/run.py --scene data/nerf/fox --save_snapshot fox.ingp --n_steps 5000
   ```

   对应 [scripts/run.py:253-255](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L253-L255)，注意第二个参数是 `False`（不带优化器状态）：

   ```python
   if args.save_snapshot:
       os.makedirs(os.path.dirname(args.save_snapshot), exist_ok=True)
       testbed.save_snapshot(args.save_snapshot, False)
   ```

2. **加载快照并继续训练**：

   ```bash
   python scripts/run.py --load_snapshot fox.ingp --n_steps 3000 --save_snapshot fox2.ingp
   ```

   对应 [scripts/run.py:124-128](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L124-L128)：

   ```python
   if args.load_snapshot:
       scene_info = get_scene(args.load_snapshot)
       if scene_info is not None:
           args.load_snapshot = default_snapshot_filename(scene_info)
       testbed.load_snapshot(args.load_snapshot)
   ```

   `get_scene` 会把内置场景名（如 `fox`）翻译成磁盘路径；若你传的是真实文件路径（`fox.ingp`），`get_scene` 返回 `None`，直接用原路径加载（见 [scripts/run.py:80-84](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L80-L84)）。

3. （进阶）若想带上优化器状态更平滑地续训，绕过 run.py 直接用 pyngp：

   ```python
   # 示例代码（非项目原有，需在 pyngp 可用的环境运行）
   import pyngp as ngp
   t = ngp.Testbed()
   t.load_snapshot("fox.ingp")            # 加载权重 + 配置
   t.save_snapshot("fox_with_opt.ingp", include_optimizer_state=True, compress=True)  # 带动量另存
   ```

   `save_snapshot` 的 pybind11 绑定见 [src/python_api.cu:563-570](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L563-L570)，三个参数 `path`/`include_optimizer_state`/`compress` 都有默认值；`load_snapshot` 绑定见 [src/python_api.cu:571](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L571)。

**需要观察的现象 / 预期结果**：

- 第二条命令训练进度条的起点应当**接近** 5000（保存时的步数），而不是 0——证明 `reset_network(false)` + `deserialize` 成功恢复了现场。
- run.py 有一个贴心逻辑：若加载了快照、没指定 `--n_steps`、又没开 GUI，默认**不训练**（假设你只想渲染/评测），见 [scripts/run.py:194-198](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L194-L198)。所以本实践显式给了 `--n_steps 3000` 才会继续训。

> 待本地验证：具体起始步数、loss 数值以你机器上的实测为准；需要先按 u1-l3 编译出 `instant-ngp` 与 `pyngp`。

#### 4.4.5 小练习与答案

**练习 1**：假如把 `load_snapshot` 里的 `reset_network(false)` 改成 `reset_network()`（默认 true），加载快照后表现会有什么不同？

**参考答案**：`reset_network()` 默认会把 `m_training_step` 归零。虽然紧接着的两行会把 `training_step` 从快照恢复回来（[src/testbed.cu:5465](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5465)），所以步数表面无差；但 `reset_network(true)` 还会清掉训练临时状态、损失滑动平均等。传 `false` 是为了最小化干扰、保留可继续训练的现场。核心不变量是「先 `reset_network` 建网、再 `deserialize` 灌权重」的顺序绝不能反。

**练习 2**：什么时候应该勾选 `w/ optimizer state`？什么时候不该？

**参考答案**：打算加载后**继续长时间训练**（尤其是马上要再训很多步）时勾上，让 Adam 动量延续、避免前几步抖动；只是拿来**渲染视频、出截图、提网格**或不打算再训时不必勾——文件更小，且不携带可能已过时的动量。`run.py` 的 `--save_snapshot` 默认不勾（`False`）。

## 5. 综合实践

把本讲四个模块串起来，做一次「配置 → 训练 → 存快照 → 换机器加载 → 续训 → 评测」的完整链路。

**任务**：

1. 用 `configs/nerf/base.json` 训练 fox（如 `python scripts/run.py --scene data/nerf/fox --n_steps 5000 --save_snapshot fox.ingp`）。
2. 用 4.1 的方法确认 `fox.ingp` 顶层同时含 `encoding/network/optimizer/loss`（配置）和 `snapshot`（权重+状态），验证「快照自包含」。
3. 模拟「换机器」：仅复制 `fox.ingp`（不带 `configs/`、不带原始数据）到另一台已编译好 instant-ngp 的机器，执行 `python scripts/run.py --load_snapshot fox.ingp --n_steps 2000`。
4. 验证：训练进度从约 5000 继续（说明 `reset_network(false)` + `deserialize` 生效）；若该机器没有 `data/nerf/fox`，观察 `load_snapshot` 在 [src/testbed.cu:5384-5398](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5384-L5398) 用快照自带的 dataset 元数据 `load_nerf(m_data_path)` 兜底——但注意此时缺真实图像，只能渲染已有视角、无法继续用图像监督。这正说明快照「带了元数据但通常不带原始图像」的边界。
5. 选做：对比 `save_snapshot(..., include_optimizer_state=True/False)` 两种 `.ingp` 的大小，以及续训前几十步 loss 曲线的平稳程度。

**预期收获**：亲手验证「快照 = 摊平配置 + 序列化权重 + 运行时状态」的自包含设计，并理解 `.ingp`/`.msgpack`/`.json` 三条解析路径为何如此分工。

> 待本地验证：本综合实践依赖可运行的 instant-ngp / pyngp 与示例数据，所有数值结果以本地实测为准。

## 6. 本讲小结

- **快照是自包含的训练现场**：`save_snapshot` 把已摊平的完整 `m_network_config` 与 `m_trainer->serialize()` 出的权重合在一个 JSON 里写盘（[src/testbed.cu:5289](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5289)），外加版本号、模式、训练步数、相机、（NeRF）密度网格等运行时状态。
- **两种磁盘格式都是 msgpack**：`.ingp` 多一层 zlib 压缩（推荐、体积小），`.msgpack` 裸 msgpack；GUI 的 Compress 复选框只在 `.ingp` 时可用（[src/testbed.cu:1914-1922](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1914-L1922)）。
- **三条解析路径分工明确**：`load_network_config` 对 `.ingp`/`.msgpack` 走 msgpack（`.ingp` 先 zlib 解压）、**不 merge parent**；对 `.json` 走 `json::parse` + `merge_parent_network_config`（[src/testbed.cu:280-309](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L280-L309)）。
- **快照必须保存配置**：还原要先用配置 `reset_network(false)` 重建同拓扑网络、再 `deserialize` 权重；又因快照不 merge parent，配置只能以摊平形态随文件携带，故自包含、可移植。
- **优化器状态决定能否平滑续训**：`include_optimizer_state` 控制是否存 Adam 动量；带上可继续训练，不带则文件更小、适合纯推理（`run.py` 默认 `False`）。
- **两个入口同一套逻辑**：GUI 的 Snapshot 面板与 pyngp 的 `save_snapshot`/`load_snapshot` 最终都汇到 testbed.cu 的同一组函数；`run.py` 用 `--load_snapshot`/`--save_snapshot` 选项驱动它们。

## 7. 下一步学习建议

- **u6-l2 相机路径与视频渲染**：快照最常见的下游用途就是加载后渲染飞行视频，可与本讲的 `--load_snapshot` + 相机路径结合。
- **u6-l4 Marching Cubes 网格提取**：从快照加载 NeRF/SDF 后提取网格，是快照的另一主要用途。
- **u7-l1 pyngp 绑定架构**：若你想用 Python 编排「训练若干步→存快照→换配置→对比」的批量实验，深入了解 `save_snapshot`/`load_snapshot` 的 pybind11 绑定细节。
- **u8-l1 多 GPU**：`load_snapshot` 末尾的 `set_all_devices_dirty()` 是多 GPU 副本同步的钩子，学完多 GPU 能补全这最后一行的含义。
- **延伸阅读**：`dependencies/tiny-cuda-nn` 中 `Trainer::serialize/deserialize` 的实现，是「权重如何变成字节、字节如何回填」的真正答案，值得在掌握本讲后跨进依赖库一探。
