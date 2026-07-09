# 文件加载与模式自动识别

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一个磁盘文件被「拖进 instant-ngp」或「写在命令行里」之后，到底走了哪条加载路径。
- 掌握 `mode_from_scene` 如何仅凭**路径是目录还是文件、文件扩展名是什么**，就把输入归类到 `Nerf / Sdf / Image / Volume` 四种模式之一。
- 看懂 `load_training_data` 如何先定模式、再 `set_mode`、最后按 `m_testbed_mode` 分发到 `load_nerf / load_mesh / load_image / load_volume`。
- 理解 `load_file` 作为「统一入口」如何用文件扩展名 + JSON 字段嗅探，把输入分流到「快照 / 网络配置 / 相机路径 / 训练数据」四类，并在拖入训练数据时自动开启训练。

本讲承接 [u2-l1 Testbed 类与四种模式](u2-l1-testbed-and-modes.md)：那里讲了 `ETestbedMode` 是什么、`set_mode` 怎么重置状态；本讲回答「**这个模式到底是谁、在什么时候、根据什么决定的**」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：四种基元对应四类完全不同的输入物。**
instant-ngp 的四种模式不是「同一个数据换套皮肤」，而是吃四种不同的输入：

| 模式 `ETestbedMode` | 典型输入物 | 加载函数 |
| --- | --- | --- |
| `Nerf` | 一个**目录**（里面是照片 + `transforms.json`） | `load_nerf` |
| `Sdf` | 一个网格文件（`.obj` / `.stl`） | `load_mesh` |
| `Image` | 一张图片（`.exr` / `.bin` / `.jpg` / `.png` / `.tga` / `.hdr`…） | `load_image` |
| `Volume` | 一个稀疏体素文件（`.nvdb`） | `load_volume` |

这张表就是本讲的「答案表」——后面所有源码都是在程序里还原这张人工归类。

**直觉二：先定模式，再建网络。**
回忆 [u2-l1](u2-l1-testbed-and-modes.md) 的核心结论：网络是「按需重建」的，`set_mode` 只是把状态清空。所以加载训练数据的正确顺序必须是 **先用 `mode_from_scene` 判定模式 → 调 `set_mode` 切到正确模式 → 再调对应的 `load_xxx` 把数据读进内存**。顺序反了，数据会读到错误模式的结构体里。

**直觉三：用户可以丢进来的东西不止「训练数据」。**
除了上面四类训练数据，用户还可能丢进来：

- 一个**快照**（`.ingp` / `.msgpack`，里面是训练好的网络参数）。
- 一份**网络配置**（`.json`，定义 encoding/network/optimizer/loss）。
- 一段**相机路径**（`.json`，含 `path` 字段的关键帧，用于导视频）。

`load_file` 之所以存在，就是为了把「用户随手丢进来的任意文件」分到这几类里。

> 名词解释：
> - **扩展名（extension）**：路径中最后一个 `.` 之后的部分，例如 `armadillo.obj` 的扩展名是 `obj`。代码里全程用「大小写不敏感」比较，所以 `.OBJ` 和 `.obj` 等价。
> - **`fs::path`**：这里不是标准库 `std::filesystem::path`，而是依赖库 `ghc::filesystem`（别名 `ngp::fs`），提供 `is_directory()`、`extension()`、`exists()` 等方法，语义与标准库一致。
> - **`equals_case_insensitive(a, b)`**：一个工具函数，把两个字符串都转小写后逐字符比较，相等返回 `true`。

## 3. 本讲源码地图

本讲只涉及三个文件，构成一条清晰的「入口 → 判定 → 分发」调用链：

| 文件 | 角色 | 关键函数 |
| --- | --- | --- |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | 中枢：承载统一入口 `load_file` 和模式分发 `load_training_data` | `load_file`、`load_training_data` |
| [src/common_host.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu) | 工具层：模式判定的纯函数实现 | `mode_from_scene`、`mode_from_string`、`to_string` |
| [include/neural-graphics-primitives/common_host.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_host.h) | 声明层：上面那些工具函数的原型 | `mode_from_scene` 等声明 |

调用关系（从上到下，越靠上越靠近用户）：

```
用户拖文件 / 命令行写文件
        │
        ▼
   load_file(path)              ← 统一入口，按扩展名+JSON内容分流
        │ (训练数据分支)
        ▼
   load_training_data(path)     ← 先定模式，再分发
        │
        ├── mode_from_scene(path)   ← 纯函数：路径→模式
        ├── set_mode(mode)          ← 切模式、清状态
        └── switch(mode)            ┐
                ├── load_nerf       │
                ├── load_mesh       │ ← 各模式专属加载函数
                ├── load_image      │   （本讲只点到调用点，不讲内部）
                └── load_volume     ┘
```

记住这张图，下面三节就是自下而上把它填满。

## 4. 核心概念与源码讲解

按「叶子函数 → 中间分发 → 顶层入口」的顺序讲，这样你读到上层时，下层已经是老朋友。

### 4.1 模式自动识别：mode_from_scene

#### 4.1.1 概念说明

`mode_from_scene` 是一个**纯函数**：输入一个路径字符串，输出一个 `ETestbedMode`，不修改任何状态、不读网络、不碰 GPU。它的职责只有一条——**根据路径形态，把输入猜成四种模式之一（或 `None`）**。

为什么是「猜」？因为 instant-ngp 支持的图片格式太多（`exr/bin/jpg/png/tga/hdr/...`），作者懒得一一列举，干脆用一个「兜底分支」：不是目录、不是 json、不是 obj/stl、不是 nvdb 的，统统当成图片。源码注释原话就是 `// probably an image.`。

#### 4.1.2 核心流程

判别规则按顺序短路求值（命中即返回）：

```
mode_from_scene(scene):
    把 scene 包成 fs::path
    if 路径不存在:           return None        # 文件都没了，无法判定
    if 是目录  或  扩展名==json:  return Nerf   # NeRF 永远是「一个文件夹」
    if 扩展名==obj 或 ==stl:      return Sdf    # 网格 → 距离场
    if 扩展名==nvdb:              return Volume # 稀疏体素
    return Image                                    # 兜底：大概是图片
```

要点：

1. **`None` 只在「路径不存在」时出现**。注意它**不**意味着「加载失败」，它只表示「无法据路径判断模式」。后续 `load_training_data` 见到 `None` 会抛异常。
2. **目录优先 → Nerf**。这是因为 NeRF 数据集天然是「照片 + transforms.json 的文件夹」，从来不是单个文件。
3. **`.json` 被判成 Nerf**。这看起来奇怪——`.json` 不是网络配置吗？关键在于**调用语境**：`mode_from_scene` 只在「加载数据」链路里被调用，而 NeRF 的训练数据入口 `transforms.json` 正是 `.json`。所以这里把 `.json` 当 NeRF 是合理的（它指的就是数据集描述文件，不是网络配置）。
4. **Image 是兜底**，靠的是「排除法」而不是白名单。

#### 4.1.3 源码精读

完整实现只有十几行：

[src/common_host.cu:L144-L160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144-L160) —— `mode_from_scene` 全身。先用 `is_directory()` 和 `extension()` 做形态判断，逐条 `equals_case_insensitive` 比较扩展名，最后兜底返回 `Image`。

```cpp
ETestbedMode mode_from_scene(const std::string& scene) {
    fs::path scene_path = scene;
    if (!scene_path.exists()) {
        return ETestbedMode::None;
    }

    if (scene_path.is_directory() || equals_case_insensitive(scene_path.extension(), "json")) {
        return ETestbedMode::Nerf;
    } else if (equals_case_insensitive(scene_path.extension(), "obj") || equals_case_insensitive(scene_path.extension(), "stl")) {
        return ETestbedMode::Sdf;
    } else if (equals_case_insensitive(scene_path.extension(), "nvdb")) {
        return ETestbedMode::Volume;
    } else { // probably an image. Too bothersome to list all supported ones: exr, bin, jpg, png, tga, hdr, ...
        return ETestbedMode::Image;
    }
}
```

配套的两个小工具函数也值得一看，它们把「模式」在字符串与枚举之间互转，后面 `configs/<mode>/` 的路径解析就靠 `to_string`：

[src/common_host.cu:L162-L185](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L162-L185) —— `mode_from_string`（字符串→枚举，用于解析命令行或配置里的 `"nerf"/"sdf"/...`）和 `to_string(ETestbedMode)`（枚举→小写字符串，例如 `Nerf→"nerf"`，正好对应 `configs/nerf/` 目录名）。

声明统一收在头文件：

[include/neural-graphics-primitives/common_host.h:L50-L52](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common_host.h#L50-L52) —— 三个函数的原型声明，`mode_from_scene` 在此对 `Testbed` 暴露。

> 提示：`mode_from_scene` 用到的 `extension()` 来自 `fs::path`（即 `ghc::filesystem`），对无扩展名的文件返回空串，于是空串既不等于 `json` 也不等于 `obj` 等，会落入 Image 兜底——但只有当路径是「存在但不带扩展名的文件」时才会走到这里。

#### 4.1.4 代码实践

**实践目标**：用仓库里真实存在的数据，验证 `mode_from_scene` 的判别表。

**操作步骤**：

1. 确认仓库里的示例数据：`data/nerf/fox`（目录）、`data/sdf/armadillo.obj`、`data/sdf/bunny.obj`、`data/image/albert.exr`。
2. 对每个路径，**只看扩展名/是否目录**，套用 4.1.2 的规则，手写预测的 `ETestbedMode`。
3. 拿预测结果与下面「预期结果」对账。

**需要观察的现象**：你会看到「目录 / json → Nerf」「obj → Sdf」「exr → Image」三条规则被这三个真实样本各命中一条。

**预期结果**：

| 输入路径 | 形态判定 | `mode_from_scene` 返回 |
| --- | --- | --- |
| `data/nerf/fox` | 目录 | `Nerf` |
| `data/sdf/armadillo.obj` | 扩展名 `obj` | `Sdf` |
| `data/image/albert.exr` | 扩展名 `exr`（非 json/obj/stl/nvdb） | `Image`（兜底分支） |

> 注意：本仓库**没有** `data/volume/` 目录，也没有 `cloud.nvdb` 文件，所以 Volume 这一支无法用现成数据验证；若你自行放入一个 `.nvdb` 文件，规则会把它判为 `Volume`（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：把 `data/nerf/fox` 改名为 `data/nerf/fox.zip`（仍然是个目录，只是名字里带点），`mode_from_scene` 会返回什么？

**答案**：仍然是 `Nerf`。因为 `is_directory()` 判断为真，**目录优先**短路，根本不会去看扩展名。

**练习 2**：如果一个路径指向一个**不存在**的文件，`mode_from_scene` 返回什么？调用方该不该把它当错误？

**答案**：返回 `None`。`None` 在本函数里只表示「无法判定」，但调用方 `load_training_data` 见到 `None` 会抛 `runtime_error("Unknown scene format ...")`，所以最终会被当错误抛出。

**练习 3**：为什么作者宁可写一个「兜底 → Image」分支，也不把所有图片格式列全？

**答案**：因为支持的图片格式多且会随 `stbi/exr/binary` 加载器扩展（见 [u5-l3](u5-l3-image-primitive.md) 的 `load_image`）。逐一列举既啰嗦又容易漏；而「不是目录/json/obj/stl/nvdb 的就当图片」这条排除法，配合后续 `load_image` 自己的格式校验，已经足够正确且更易维护。

---

### 4.2 训练数据加载：load_training_data 的模式分发

#### 4.2.1 概念说明

`load_training_data` 是「加载数据」链路的核心。它把上一节的纯判定函数 `mode_from_scene` 和 [u2-l1](u2-l1-testbed-and-modes.md) 的 `set_mode` 串起来，再做一次 `switch` 把控制权交给模式专属的 `load_xxx`。

它解决的三个问题：

1. **模式还没定**：构造完 `Testbed`、模式还是 `None` 时，谁来决定该进哪条路？——由 `mode_from_scene` 决定。
2. **状态是上一个模式残留的**：切换模式前必须清空旧模式的 `m_nerf/m_sdf/m_image/m_volume` 等成员。——由 `set_mode` 负责。
3. **数据怎么读进来**：交给 `load_nerf/load_mesh/load_image/load_volume`。

注意它的「兄弟」函数：`reload_training_data()`（L184）只是用记下来的 `m_data_path` 再调一次自己；`clear_training_data()`（L190）只把 `m_training_data_available` 置 false。它们都围绕同一个 `load_training_data` 转。

#### 4.2.2 核心流程

```
load_training_data(path):
    if !path.exists():                        throw "Data path does not exist"
    scene_mode = mode_from_scene(path)        # ① 判定模式
    if scene_mode == None:                    throw "Unknown scene format"
    set_mode(scene_mode)                      # ② 切模式（清旧状态、设默认值）
    m_data_path = path                        # ③ 记下数据路径（供 reload / GUI）
    switch (m_testbed_mode):                  # ④ 按模式分发
        Nerf:   load_nerf(path)
        Sdf:    load_mesh(path)
        Image:  load_image(path)
        Volume: load_volume(path)
        else:   throw "Invalid testbed mode"
    m_training_data_available = true          # ⑤ 标记数据已就绪
    update_imgui_paths()                      # ⑥ 刷新 GUI 里的路径文本框
```

要点：

- **顺序不能换**：必须先 `set_mode` 再 `load_xxx`。`set_mode` 会清空 `m_nerf` 等结构体（见 [u2-l1](u2-l1-testbed-and-modes.md)），若先 `load_nerf` 再 `set_mode`，刚读进去的数据会被立刻清掉。
- `set_mode` 内部对「相同模式」是早退的（`if (mode == m_testbed_mode) return;`），所以重复加载同一模式不会触发重置——这是性能与正确性的双重保护。
- `m_data_path` 在 ③ 被保存，之后 GUI 的「Reload」按钮、`reload_training_data()` 都用它。

#### 4.2.3 源码精读

[src/testbed.cu:L156-L182](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L156-L182) —— `load_training_data` 全身。注意注释点明「Automatically determine the mode from the first scene that's loaded」（从第一个被加载的场景自动确定模式）。

```cpp
void Testbed::load_training_data(const fs::path& path) {
    if (!path.exists()) {
        throw std::runtime_error{fmt::format("Data path '{}' does not exist.", path.str())};
    }

    // Automatically determine the mode from the first scene that's loaded
    ETestbedMode scene_mode = mode_from_scene(path.str());
    if (scene_mode == ETestbedMode::None) {
        throw std::runtime_error{fmt::format("Unknown scene format for path '{}'.", path.str())};
    }

    set_mode(scene_mode);

    m_data_path = path;

    switch (m_testbed_mode) {
        case ETestbedMode::Nerf:   load_nerf(path);   break;
        case ETestbedMode::Sdf:    load_mesh(path);   break;
        case ETestbedMode::Image:  load_image(path);  break;
        case ETestbedMode::Volume: load_volume(path); break;
        default: throw std::runtime_error{"Invalid testbed mode."};
    }

    m_training_data_available = true;
    update_imgui_paths();
}
```

这就是「四种基元共用同一套加载骨架」的体现：判定与分发只写一次，真正不同的 `load_xxx` 各自住在 `testbed_nerf.cu / testbed_sdf.cu / testbed_image.cu / testbed_volume.cu` 里（后续单元细讲）。

它被两个地方调用，可以印证「数据入口」的统一性：

[src/main.cu:L157-L159](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L157-L159) —— 命令行 `--scene` 标志直接调 `load_training_data`。注意：CLI 里 `--scene` 走的是 `load_training_data`（纯数据入口），而位置参数走的是更上层的 `load_file`（见 4.3）。

```cpp
if (scene_flag) {
    testbed.load_training_data(get(scene_flag));
}
```

[src/python_api.cu:L452](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L452) —— pyngp 也把 `load_training_data` 暴露成 Python 方法（释放 GIL），所以 `pyngp.Testbed(...).load_training_data(path)` 走的是同一条路。

#### 4.2.4 代码实践

**实践目标**：亲手跑一次 NeRF 数据加载，从日志确认模式被自动判定为 `Nerf`。

**操作步骤**：

1. 编译出可执行文件（见 [u1-l3](u1-l3-build-system.md)）。
2. 运行无头模式加载 fox：`./instant-ngp data/nerf/fox --no-gui`（或带 GUI 直接 `./instant-ngp data/nerf/fox`）。
3. 观察启动日志里关于 mode / network 的输出行。

**需要观察的现象**：程序不会报「Unknown scene format」，且后续会加载 `configs/nerf/base.json`（因为模式已是 `Nerf`，`to_string` 把它拼成了 `nerf`）。

**预期结果**：训练正常开始，日志出现类似 `Loading network config from: .../configs/nerf/base.json`。如果你故意指向一个不存在的路径，例如 `./instant-ngp data/nerf/does_not_exist`，会看到 `mode_from_scene` 返回 `None` 触发的异常信息。**待本地验证**：不同平台/编译选项下日志措辞可能略有差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_training_data` 里要先 `set_mode(scene_mode)`，再去 `switch(m_testbed_mode)` 分发？直接 `switch(scene_mode)` 不行吗？

**答案**：功能上 `switch(scene_mode)` 也能正确分发，但会**漏掉 `set_mode` 的副作用**——清空旧模式成员、设置模式相关默认值（如多 GPU 仅在 NeRF 启用 `m_use_aux_devices`、重置相机）。不调 `set_mode`，`m_testbed_mode` 仍是旧值，后续 `train()/render()` 的 `switch(m_testbed_mode)` 分发就会走错分支。所以必须先把 `m_testbed_mode` 设对。

**练习 2**：`reload_training_data()` 是怎么实现的？它和 `load_training_data` 是什么关系？

**答案**：见 [src/testbed.cu:L184-L188](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L184-L188)：它只在 `m_data_path` 存在时，用这个保存下来的路径再调一次 `load_training_data(m_data_path)`。它是「用上次的路径重新加载」的便捷封装。

**练习 3**：`load_training_data` 把 `m_training_data_available` 置为 `true` 放在 `switch` 之后。如果 `load_nerf` 内部抛了异常，这个标志会被置 true 吗？

**答案**：不会。异常会沿调用栈传播、跳出 `load_training_data`，`switch` 之后的 `m_training_data_available = true;` 不会执行。这正是想要的行为——加载失败就不该声称「数据已就绪」。

---

### 4.3 顶层入口：load_file 的多类型分发

#### 4.3.1 概念说明

`load_file` 是用户文件的**统一入口**。无论是命令行的位置参数，还是 GUI 里把文件拖进窗口，最终都汇聚到它。它的职责比 `load_training_data` 大：`load_training_data` 只管「训练数据」，而 `load_file` 还要识别「快照 / 网络配置 / 相机路径」这三类非数据文件。

它的判别策略是**两层嗅探**：

1. **第一层：看扩展名**——`.ingp`/`.msgpack` 几乎一定是快照，直接交给 `load_snapshot`。
2. **第二层（针对 `.json`）：打开文件嗅探字段**——`.json` 可能是三样东西之一，靠它包含的顶层字段来区分：
   - 含 `"snapshot"` → 其实是个快照（json 格式的快照，低效但支持）。
   - 含 `"parent"`/`"network"`/`"encoding"`/`"loss"`/`"optimizer"` 任一 → 网络配置。
   - 含 `"path"` → 相机路径。
3. **兜底：都不是，就当训练数据**，交给 `load_training_data`（也就是 4.2 那条链）。此时若之前没有训练数据，会**自动开启训练** `m_train = true`。

#### 4.3.2 核心流程

```
load_file(path):
    if !path.exists():
        if 扩展名==json 且 find_network_config(path) 能解析到:   reload_network_from_file(path); return
        else: 报错 "File does not exist"; return

    if 扩展名==ingp 或 msgpack:        load_snapshot(path); return      # 快照

    if 扩展名==json:
        解析 JSON
        if 含 "snapshot":              load_snapshot(path); return       # json 快照
        if 含 parent/network/encoding/loss/optimizer:  reload_network_from_file(path); return  # 网络配置
        if 含 "path":                  load_camera_path(path); return    # 相机路径

    # 兜底：当作训练数据
    记下 was = m_training_data_available
    load_training_data(path)                                  # → 走 4.2 的链
    if !was:  m_train = true                                  # 之前没数据 → 自动开训练
```

两个值得注意的设计：

- **不存在的 `.json` 仍可能有用**：路径不存在时，若它是 `.json` 且能被 `find_network_config` 在 `configs/<mode>/` 下解析到（例如只写了 `base.json`），就当作「想换网络配置」处理。这正是你写 `--network base.json` 时不必写全路径的原因。
- **自动开训练的智慧**：用户既然把训练数据拖进来了，几乎肯定想立刻训练，所以程序替你按下 `T` 键（`m_train = true`）。但只在「之前没有任何训练数据」时这么做，避免打断已有训练。

#### 4.3.3 源码精读

[src/testbed.cu:L353-L410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L353-L410) —— `load_file` 全身。注意末尾 `// If the dragged file isn't any of the above, assume that it's training data` 这段兜底与自动开训练逻辑。

```cpp
void Testbed::load_file(const fs::path& path) {
    if (!path.exists()) {
        // If the path doesn't exist, but a network config can be resolved, load that.
        if (equals_case_insensitive(path.extension(), "json") && find_network_config(path).exists()) {
            reload_network_from_file(path);
            return;
        }
        tlog::error() << "File '" << path.str() << "' does not exist.";
        return;
    }

    if (equals_case_insensitive(path.extension(), "ingp") || equals_case_insensitive(path.extension(), "msgpack")) {
        load_snapshot(path);
        return;
    }

    // If we get a json file, we need to parse it to determine its purpose.
    if (equals_case_insensitive(path.extension(), "json")) {
        json file;
        {
            std::ifstream f{native_string(path)};
            file = json::parse(f, nullptr, true, true);
        }

        // Snapshot in json format... inefficient, but technically supported.
        if (file.contains("snapshot")) { load_snapshot(path); return; }

        // Regular network config
        if (file.contains("parent") || file.contains("network") || file.contains("encoding") ||
            file.contains("loss") || file.contains("optimizer")) {
            reload_network_from_file(path);
            return;
        }

        // Camera path
        if (file.contains("path")) { load_camera_path(path); return; }
    }

    // If the dragged file isn't any of the above, assume that it's training data
    try {
        bool was_training_data_available = m_training_data_available;
        load_training_data(path);
        if (!was_training_data_available) {
            // ...the user wants to immediately start training.
            m_train = true;
        }
    } catch (const std::runtime_error& e) { tlog::error() << "Failed to load training data: " << e.what(); }
}
```

「统一入口」的证据有两处。第一处是命令行位置参数：

[src/main.cu:L153-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L153-L155) —— 命令行里所有位置参数（不带 `--` 的文件）逐个喂给 `load_file`，由它自己判断类型。

```cpp
for (auto file : get(files)) {
    testbed.load_file(file);
}
```

第二处是 GUI 拖拽回调：

[src/testbed.cu:L3674-L3690](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3674-L3690) —— GLFW 的拖拽回调 `glfwSetDropCallback`，对每个被拖入的路径调 `testbed->load_file(paths[i])`（先让 `m_file_drop_callback` 尝试处理）。这正是「拖文件进窗口」的入口。

第三处是 pyngp：

[src/python_api.cu:L574-L575](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L574-L575) —— pyngp 也把 `load_file` 暴露成方法，Python 侧可用 `testbed.load_file(path)` 享受同样的自动分流。

> 补充：`load_file` 在「不存在的 `.json`」分支里用到的 `find_network_config`（[src/testbed.cu:L254-L270](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L254-L270)）会把相对路径补成 `root_dir()/configs/<mode>/<文件名>`，其中 `<mode>` 就是 `to_string(m_testbed_mode)`。配置路径解析的细节留到 [u2-l4 网络配置体系](u2-l4-network-config.md)。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `load_file` 对 `.json` 的「字段嗅探」分流——同一扩展名、不同内容，走不同分支。

**操作步骤**：

1. 准备两份最小 `.json` 文件（**示例代码**，非项目原有文件）：
   - `net.json`：`{"network":{"otype":"FullyFusedMLP"}}` —— 含 `network` 字段，应被判为网络配置。
   - `cam.json`：`{"path":[]}` —— 含 `path` 字段，应被判为相机路径。
2. 在 GUI 模式下分别把这两个文件拖进窗口（或在命令行作为位置参数传入 `./instant-ngp data/nerf/fox net.json`）。
3. 观察程序行为差异。

**需要观察的现象**：
- 拖 `net.json`：触发 `reload_network_from_file`，随后 `reset_network()` 重建网络（日志可见网络重载）。
- 拖 `cam.json`：触发 `load_camera_path`，GUI 的 Camera path 面板会载入该路径（不重建网络）。

**预期结果**：两份文件扩展名都是 `.json`，但因为顶层字段不同，被分流到完全不同的处理函数。这印证了 `load_file`「扩展名 + 字段」的两层嗅探策略。**待本地验证**：相机路径若为空数组，GUI 不一定报错但不产生可视效果；可换成真实相机路径 `.json` 观察。

#### 4.3.5 小练习与答案

**练习 1**：用户拖入一个 `.ingp` 快照文件，`load_file` 会进入哪个分支？会触发 `load_training_data` 吗？

**答案**：进入 `equals_case_insensitive(path.extension(), "ingp")` 分支，调用 `load_snapshot(path)` 然后 `return`。**不会**触发 `load_training_data`——快照里自带网络参数和（可能的）训练状态，不需要重新加载训练数据。

**练习 2**：为什么兜底分支里要用 `try/catch(std::runtime_error)` 包住 `load_training_data`？

**答案**：因为「兜底」只是 `load_file` 的猜测——文件可能既不是快照/配置/相机路径，也不是合法训练数据（比如损坏的文件、未知格式）。`load_training_data` 在这些情况下会抛 `runtime_error`（路径不存在 → None → "Unknown scene format"）。`load_file` 作为 GUI/CLI 入口，吞掉异常并 `tlog::error()` 记录，避免一次失败的拖拽让整个程序崩溃。

**练习 3**：命令行同时写了位置参数 `./instant-ngp data/nerf/fox base.json`，这两个文件分别走哪个函数？

**答案**：都走 `load_file`（[main.cu:L153-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L153-L155) 的循环）。`fox`（目录）在 `load_file` 里走兜底 → `load_training_data` → `load_nerf`；`base.json` 含（继承后得到的）network/encoding 等字段 → `reload_network_from_file`。注意顺序：先加载数据定了模式，后加载的配置才能被 `find_network_config` 在 `configs/nerf/` 下正确解析。

---

## 5. 综合实践

把三个最小模块串起来，完成规格里给定的「四文件路由表」任务。

**任务**：给定四个文件路径——`data/nerf/fox/`（目录）、`data/sdf/armadillo.obj`、`data/image/albert.exr`、`cloud.nvdb`——分别写出：

1. 若通过 `--scene`（即直接进 `load_training_data`）：`mode_from_scene` 返回什么、`set_mode` 切到哪个模式、`switch` 分发到哪个 `load_xxx`。
2. 若通过命令行位置参数（即进 `load_file`）：会先经过哪些扩展名/字段判断，最终落到哪个函数。

**操作步骤**：

1. 逐个文件，按 `mode_from_scene`（4.1）规则先定模式。
2. 套用 `load_training_data` 的 `switch`（4.2）写出 `load_xxx`。
3. 对 `load_file`（4.3）路径，判断是否会命中快照/json 分支，还是落到兜底 `load_training_data`。

**预期结果（路由表）**：

| 输入 | `mode_from_scene` | `set_mode` → 模式 | `load_training_data` 分发 | 经 `load_file` 的最终落点 |
| --- | --- | --- | --- | --- |
| `fox/`（目录） | `Nerf` | `Nerf` | `load_nerf` | 兜底 → `load_training_data` → `load_nerf` |
| `armadillo.obj` | `Sdf` | `Sdf` | `load_mesh` | 兜底 → `load_training_data` → `load_mesh` |
| `albert.exr` | `Image`（兜底） | `Image` | `load_image` | 兜底 → `load_training_data` → `load_image` |
| `cloud.nvdb` | `Volume` | `Volume` | `load_volume` | 兜底 → `load_training_data` → `load_volume` |

四个文件**都不**是 `.ingp/.msgpack/.json`，所以在 `load_file` 里全部跳过前几个分支，落到兜底的 `load_training_data`，再由它内部的 `mode_from_scene + switch` 完成实际分发——这就是「`load_file` 是外壳、`load_training_data` 是数据内核」的关系。

**待本地验证**：`cloud.nvdb` 在本仓库并不存在（无 `data/volume/`），无法实跑；但据 `mode_from_scene` 第 154 行的 `.nvdb→Volume` 规则，路由结论是确定的。前三个文件可用仓库自带数据实跑验证。

## 6. 本讲小结

- `mode_from_scene`（[src/common_host.cu:L144-L160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L144-L160)）是纯函数，按「目录/json→Nerf、obj/stl→Sdf、nvdb→Volume、其余→Image」的短路规则把路径猜成模式；路径不存在才返回 `None`。
- `load_training_data`（[src/testbed.cu:L156-L182](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L156-L182)）是数据加载内核：`mode_from_scene` 定模式 → `set_mode` 清旧状态并切模式 → `switch` 分发到 `load_nerf/load_mesh/load_image/load_volume`。顺序「先切模式后加载数据」不可换。
- `load_file`（[src/testbed.cu:L353-L410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L353-L410)）是用户文件的统一入口，CLI 位置参数、GUI 拖拽、pyngp 都汇于此；它用「扩展名 + JSON 字段嗅探」把文件分流到快照 / 网络配置 / 相机路径 / 训练数据四类。
- 训练数据是 `load_file` 的兜底分支：一旦拖入数据且之前无数据，自动置 `m_train = true` 替用户按下训练键。
- 四种基元共用同一套加载骨架（判定 + 切模式 + 分发），真正不同的 `load_xxx` 各自住在 `testbed_*.cu` 里，构成后续各原语单元的入口。
- 异常处理有层次：`mode_from_scene` 只返回 `None` 不抛错，`load_training_data` 把 `None` 升级成异常，`load_file` 再用 `try/catch` 把异常降级成日志，保证一次失败的拖拽不会拖垮整个程序。

## 7. 下一步学习建议

- 紧接着读 [u2-l4 网络配置体系：JSON 与继承](u2-l4-network-config.md)，弄清本讲多次提到的 `find_network_config` / `reload_network_from_file` / `load_network_config` 如何解析 `configs/<mode>/` 路径、如何用 `parent` 做配置继承。
- 想看「数据内核」之后的细节，可跳到对应原语单元：NeRF 数据格式见 [u4-l1 NeRF 数据集与 transforms.json](u4-l1-nerf-dataset.md)；网格/距离场见 [u5-l1 SDF 原语与球面追踪](u5-l1-sdf-sphere-tracing.md)；图片见 [u5-l3 图像原语](u5-l3-image-primitive.md)；体素见 [u5-l4 体素原语与 NanoVDB](u5-l4-volume-primitive.md)。
- 想理解快照的内部结构（`load_snapshot` 在本讲只是被调用），可读 [u6-l3 快照：保存与加载训练结果](u6-l3-snapshots.md)。
- 建议动手：在 `src/testbed.cu` 的 `load_file` 兜底分支临时加一行 `tlog::info() << "auto-route to training data: " << path;`（**只读练习，勿提交**），重新编译后拖入不同类型文件，从日志直观感受分流过程。
