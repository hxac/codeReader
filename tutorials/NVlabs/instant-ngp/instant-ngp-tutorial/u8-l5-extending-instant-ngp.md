# 扩展 instant-ngp：自定义配置与二次开发

## 1. 本讲目标

本讲是整本手册的收尾篇。前面七单元我们已经把 instant-ngp 从「跑起来」一路拆到「NeRF/SDF/图像/体素四种原语、渲染产物、Python 自动化、多 GPU、JIT 融合、相机自标定、DLSS/VR」。本讲不再讲解新的内部机制，而是退后一步，回答一个面向二次开发者的总问题：

**「我想改一改 instant-ngp 的行为，到底该从哪里下手？」**

读完本讲，你应当能够：

- 掌握**只改 `configs/*.json`、不重新编译**就能做超参实验的标准做法（配置调参）。
- 掌握用 **pyngp** 编写可复现、自动化的受控实验脚本，能对比不同配置下的参数量与损失曲线（pyngp 实验）。
- 认清 instant-ngp（应用层）与 tiny-cuda-nn（库层）之间的**职责边界**，知道新增一个编码/损失/优化器该改哪个仓库（tiny-cuda-nn 扩展边界）。

本讲覆盖三个最小模块：**配置调参、pyngp 实验、tiny-cuda-nn 扩展边界**。

## 2. 前置知识

本讲是高级收尾篇，默认你已掌握以下已建立的认知（本讲会直接承接，不再重复）：

- **配置体系**（u2-l4）：`configs/<模式>/*.json` 的 `encoding/network/optimizer/loss` 四大块、`parent` 继承与 `merge_parent_network_config` 的深度合并、`find_network_config` 把简写补全为完整路径。
- **编码对比**（u3-l4）：`HashGrid/Frequency/DenseGrid/Identity` 四种输入编码的结构差异、`otype` 字段、以及 `reset_network` 里的自动参数推导（含 `per_level_scale`）只对 `otype` 含 `grid`/`permuto` 的编码生效。
- **reset_network**（u3-l1）：`m_loss/m_optimizer/m_encoding/m_network/m_trainer` 五大对象按依赖顺序由 tiny-cuda-nn 工厂构造。

如果你对这些还不熟，建议先回去读对应讲义。下面用一句话回顾本讲最关键的两个事实：

1. instant-ngp 是**应用层**：它只负责解析 JSON、调度训练/渲染、组织四种基元的业务逻辑；它本身不实现任何神经网络算子。
2. 真正的神经网络算法（哈希编码、MLP 内核、优化器、损失）全部来自外部依赖 **tiny-cuda-nn**，instant-ngp 通过工厂函数（`create_loss`/`create_optimizer`/`create_encoding`/`create_network`）去取用它们。

这条「应用层 ↔ 库层」边界，就是本讲判断「该改哪里」的总纲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `configs/nerf/base.json` | NeRF 默认配置，本讲所有调参的「基准」与 `parent` 父配置 |
| `configs/nerf/small.json`、`big.json`、`densegrid.json`、`tensor.json` | 官方提供的调参范例：分别演示改哈希表大小、改编码、用 Composite 编码 |
| `src/testbed.cu` | `reset_network()`（消费配置建网）与 `find_network_config`/`reload_network_from_file`/`reload_network_from_json`（配置加载链） |
| `src/python_api.cu` | pyngp 绑定：`reload_network_from_json`、`n_params`、`n_encoding_params`、`loss`、`training_step` 等实验关键 API |
| `include/neural-graphics-primitives/testbed.h` | `m_n_levels`/`m_per_level_scale`/`m_loss_scalar`/`m_loss_graph` 等被实验脚本读取的成员 |
| `README.md` | 官方明确指出的「二次开发请用 tiny-cuda-nn PyTorch 扩展」边界声明 |

## 4. 核心概念与源码讲解

### 4.1 配置调参：不重新编译的实验循环

#### 4.1.1 概念说明

instant-ngp 把「网络长什么样」完全外置到 JSON 配置里：编码类型、哈希表大小、MLP 宽度深度、学习率、损失函数……全部是数据，不是代码。这意味着绝大多数超参实验**不需要改一行 C++、不需要重新编译**，只要改一个 JSON 文件即可。

这套机制的威力来自 `parent` 继承（u2-l4 已讲）：你不必每次复制整份 `base.json`，只要写一个只含你想改的那几个字段、并指向 `base.json` 的小配置即可。仓库自带的 `configs/nerf/` 下就有一批这样的范例，本身就是「官方调参示例」，是我们学习调参的最佳教材。

#### 4.1.2 核心流程

一次配置调参的标准流程：

1. 选定基准配置（通常是 `base.json`）。
2. 新建一个子配置，写 `"parent": "base.json"`，再只列出要覆盖的字段。
3. 用 `--network myconfig.json`（CLI）或 `reload_network_from_file("myconfig.json")`（pyngp）加载它。
4. `find_network_config` 把简写补全为 `configs/<模式>/myconfig.json` 完整路径。
5. `load_network_config` 用 `json::parse` 读入后，`merge_parent_network_config` 按 RFC 7396 合并父配置（对象键递归合并、非对象子覆盖父）。
6. 因为不是快照，`reload_network_from_file` 触发 `reset_network()` 重建网络、**从头训练**。
7. 观察启动日志里的 `MultiLevelEncoding:` 行（打印解析后的 `L/b/T/F`）与参数量，对比训练效果。

关键点：**改的是参数值（已存在的字段），不是新增算法类型**。后者属于 4.3 的边界问题。

#### 4.1.3 源码精读

先看基准配置 `base.json` 的编码块，这是 NeRF 默认的哈希编码参数：

[configs/nerf/base.json:L23-L29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29) —— `HashGrid` 编码，`n_levels=8`、每层 4 维特征、`log2_hashmap_size=19`（即哈希表 \(2^{19}\) 项）、`base_resolution=16`。

再看官方是怎么「只改一个字段」做调参的。缩小哈希表（参数更少、更快但细节更弱）：

[configs/nerf/small.json:L1-L12](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/small.json#L1-L12) —— 仅把 `log2_hashmap_size` 从 19 改成 15（哈希表小了 \(2^{4}=16\) 倍），并把 `ExponentialDecay` 的 `decay_start`/`decay_interval` 减半以匹配更快收敛。

放大哈希表（参数更多、细节更强、更慢）：

[configs/nerf/big.json:L1-L6](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/big.json#L1-L6) —— 仅把 `log2_hashmap_size` 从 19 改成 21。

> 这两份文件加起来不到 20 行，却完整定义了「小模型 / 大模型」两个对照实验——这正是 `parent` 继承的价值。

`log2_hashmap_size` 每加 1，HashGrid 编码的可学习参数翻倍；它由 `reset_network` 透传给 tiny-cuda-nn。来看 `reset_network` 如何消费这些字段——下面这段对 `otype` 含 `grid`/`permuto` 的编码做自动参数推导：

[src/testbed.cu:L4217-L4260](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4217-L4260) —— 它做了三件与调参直接相关的事：

1. 读 `log2_hashmap_size`（缺省 15）与 `base_resolution`；若没给 `base_resolution`，则按 \(2^{\lfloor \text{log2\_hashmap\_size}/n_{\text{pos}}\rfloor}\) 自动推一个。
2. 读 `per_level_scale`；**若你显式给了一个正值，就用你的值**；否则按
   \[
   b=\exp\!\left(\frac{\ln(N_{\max}\cdot \text{aabb\_scale}\,/\,N_{\min})}{L-1}\right)
   \]
   自动算（`desired_resolution` 对 NeRF 取 2048）。
3. 打印 `MultiLevelEncoding: type=... Nmin=... b=... F=... T=2^... L=...` 这一行——**这就是你调参后核对「实际生效参数」的权威日志**。

这条日志行在 [src/testbed.cu:L4257-L4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4257-L4259)。

#### 4.1.4 代码实践

**实践目标**：亲手用「子配置覆盖一个字段」的方式做一个调参实验，并核对实际生效的参数。

**操作步骤**：

1. 复制 `base.json` 的一个子配置到新文件 `configs/nerf/mytune.json`，内容如下（把哈希层从 8 增到 16、哈希表缩到 17）：

   ```json
   {
       "parent": "base.json",
       "encoding": {
           "n_levels": 16,
           "log2_hashmap_size": 17
       }
   }
   ```

2. 运行 `./instant-ngp data/nerf/fox --network mytune.json`（无 GUI 环境加 `--no-gui`）。
3. 观察启动日志中的 `MultiLevelEncoding:` 行。

**需要观察的现象**：日志应显示 `L=16 T=2^17`（你的覆盖值生效），以及 `b=...`（因为你没给 `per_level_scale`，它被自动重算，且因 `n_levels` 变大，`b` 会比 `base.json` 的默认值更小）。

**预期结果**：`n_levels` 与 `log2_hashmap_size` 被你覆盖，`per_level_scale` 被自动推导。启动日志中 `total_encoding_params` 会因为 `L` 翻倍而显著增加，又因 `T` 从 \(2^{19}\) 降到 \(2^{17}\) 而部分抵消——你可以对照观察净变化。

**待本地验证**：具体参数量数字需以你本机启动日志为准（依赖 `n_features_per_level` 与是否对齐 padding）。

#### 4.1.5 小练习与答案

**练习 1**：如果你想做一个「学习率减半」的实验，应该新建一个什么样的子配置？

**参考答案**：利用 `parent` 继承与深度合并，只要覆盖优化器最深处的 `learning_rate`：

```json
{
    "parent": "base.json",
    "optimizer": {
        "nested": { "nested": { "learning_rate": 5e-3 } }
    }
}
```

`base.json` 的 optimizer 是 `Ema → ExponentialDecay → Adam` 三层 `nested`（见 [configs/nerf/base.json:L5-L22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L5-L22)），`Adam` 在最内层，所以路径是 `optimizer.nested.nested.learning_rate`。`frequency.json` 正是用同样的手法改学习率的范例（见 [configs/nerf/frequency.json:L5-L9](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/frequency.json#L5-L9)）。

**练习 2**：`densegrid.json` 里为什么要**显式**写 `per_level_scale`，而不是像 `base.json` 那样省略？

**参考答案**：见 [configs/nerf/densegrid.json:L1-L9](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/densegrid.json#L1-L9) 与其注释。`DenseGrid` 存**全部**顶点特征，参数量随分辨率立方增长；若让 `reset_network` 自动推 `per_level_scale`（把最细层推到 2048），参数会爆炸。作者因此手工挑了 `1.405` 这个较小的比，把总参数压到「33m 的一半以内」。这也印证了 4.1.3 的规则：**显式正值优先于自动推导**。

### 4.2 pyngp 实验：可复现的自动化对照

#### 4.2.1 概念说明

配置调参解决了「怎么改」，但每次实验都要手敲命令行、肉眼读日志、手动记录数字，既慢又不可复现。pyngp（u7-l1 已讲绑定架构）把整个 Testbed 暴露给 Python，让你用脚本驱动实验：**程序化地生成不同配置、训练、记录参数量与损失曲线、画图对比**。

对实验而言，pyngp 有一个特别重要的能力：`reload_network_from_json(dict, ...)` 可以直接吃一个 Python 字典，**无需把配置写到磁盘文件**。这意味着你可以在循环里 `copy.deepcopy` 一份基准配置、改一个键、立刻重建网络从头训——这正是「受控实验」的原子操作。

#### 4.2.2 核心流程

一次 pyngp 对照实验的骨架：

1. `import pyngp as ngp`，构造 `Testbed(ngp.TestbedMode.Nerf)`。
2. `load_training_data(scene)`，置 `shall_train = True`。
3. 准备一份基准配置字典 `base_cfg`（可以先 `reload_network_from_file("base.json")`，或自己读 JSON）。
4. 对每个待对比的超参取值 `v`：
   - `cfg = copy.deepcopy(base_cfg)`，修改 `cfg["encoding"]["log2_hashmap_size"] = v`（或 `n_levels`）。
   - `reload_network_from_json(cfg)` —— 它内部会 `merge_parent_network_config` 再 `reset_network()`，**网络被重建、从头训练**。
   - 读 `n_params()` / `n_encoding_params()` 记录参数量。
   - 循环 `frame()`，每若干帧采样一次 `(training_step, loss)`，记录损失曲线。
5. 多组曲线画在一起对比。

关键 API 与返回值（全部来自 pybind11 绑定）：

- `n_params()`、`n_encoding_params()`：可训练参数总数 / 编码部分参数数。
- `loss`：属性，当前 EMA 损失标量（时间窗口 100 的指数滑动平均）。
- `training_step`：属性，当前训练步数。
- `reload_network_from_json(json, config_base_path="")`：传字典重建网络。

#### 4.2.3 源码精读

先看实验最核心的「传字典重建」绑定与签名：

[src/python_api.cu:L544-L550](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L544-L550) —— `reload_network_from_json` 暴露给 Python，第一个参数是 JSON 对象（经 pybind11_json 把 Python dict 自动转成 `nlohmann::json`），第二个 `config_base_path` 用于解析该 dict 里可能的 `parent`。

它的 C++ 实现非常短，却是整个实验循环的关键：

[src/testbed.cu:L346-L351](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L346-L351) —— 先 `merge_parent_network_config`（摊平 parent 链），再无条件 `reset_network()`。注意：**不管你传的是不是快照，这条路径都会从头重建并归零训练**——这正是受控实验想要的「每次都干净起点」。

读取参数量的两个方法绑定：

[src/python_api.cu:L561-L562](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L561-L562) —— `n_params` 与 `n_encoding_params`，分别返回总参数与编码参数。这是脚本里做「参数量 vs 表达力」权衡的依据。

记录损失曲线的两个属性：

[src/python_api.cu:L669-L670](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L669-L670) —— `loss`（只读，返回 `m_loss_scalar.val()`）与 `training_step`（只读）。它们背后是：

[include/neural-graphics-primitives/testbed.h:L1088-L1091](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1088-L1091) —— `m_training_step` 步数计数器、`m_loss_scalar` 是 `Ema<float>{Time,100}` 的滑动平均损失。注意 `loss` 读到的是平滑后的值，适合画趋势曲线；如需逐脉冲原始值，C++ 侧另有 `m_loss_graph`（256 点历史，[testbed.h:L1091](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1091)），但未直接暴露给 Python。

#### 4.2.4 代码实践

**实践目标**：用 pyngp 写一个最小脚本，加载 fox、打印参数量、记录一条损失曲线。

**操作步骤**：把下面这段（示例代码，非项目原有脚本）保存为 `exp_loss.py` 并运行：

```python
# 示例代码
import pyngp as ngp

testbed = ngp.Testbed(ngp.TestbedMode.Nerf)
testbed.load_training_data("data/nerf/fox")
testbed.shall_train = True
testbed.reload_network_from_file("base.json")   # 走 find_network_config 解析

print("total params      :", testbed.n_params())
print("encoding params   :", testbed.n_encoding_params())

loss_curve = []
target_step = 1000
while testbed.training_step < target_step:
    testbed.frame()
    if testbed.training_step % 50 == 0:
        loss_curve.append((testbed.training_step, testbed.loss))
```

**需要观察的现象**：程序先打印两组参数量，然后随训练推进，`loss` 应单调下降（带噪声）。

**预期结果**：`n_params()` 约为百万级、`n_encoding_params()` 占其中绝大部分（因为哈希表是参数大头，MLP 很小）。`loss_curve` 是一条下降曲线。

**待本地验证**：精确参数量与曲线形状依赖你的 GPU 与随机种子。

#### 4.2.5 小练习与答案

**练习 1**：为什么在对比实验里，每次切换配置后**必须**调用 `reload_network_from_json`，而不是只改字典就接着 `frame()`？

**参考答案**：改字典只是改了 Python 侧的 `nlohmann::json` 对象，GPU 上的 `m_trainer/m_encoding/m_network` 仍是按旧配置建的。只有 `reload_network_from_json` → `reset_network()` 才会真正销毁旧网络、按新配置重建并把 `m_training_step` 归零（见 [src/testbed.cu:L346-L351](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L346-L351) 与 [src/testbed.cu:L4160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4160)）。否则你测的是「旧网络继续训」，而非「新配置从头训」，对照实验无效。

**练习 2**：`loss` 属性为什么是平滑后的 EMA 值，而不是单步原始损失？

**参考答案**：NeRF 每步只在小批量光线上算损失，单步值方差极大、上下跳动剧烈，直接画线看不出趋势。`m_loss_scalar` 用时间窗口 100 的 EMA 平滑（[testbed.h:L1090](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1090)），让曲线可读。代价是它对突变有滞后，所以在 4.2.4 里我们按步数间隔采样而非每帧采样。

### 4.3 tiny-cuda-nn 扩展边界：该改哪个仓库

#### 4.3.1 概念说明

前两个模块做的都是「在已有能力范围内调参」。但如果你的需求是**新增一种编码、新增一种损失函数、新增一种优化器，或者把哈希编码 + 快速 MLP 用到一个全新任务上**，就撞上了 instant-ngp 与 tiny-cuda-nn 之间的硬边界。

这条边界由 README 明确声明，可以总结成一张「该改哪里」判定表：

| 你的目标 | 改哪里 | 需要重新编译？ |
| --- | --- | --- |
| 改超参值（`learning_rate`、`n_levels`、`log2_hashmap_size`） | instant-ngp 的 `configs/*.json` | 否 |
| 切换**已有的**编码/损失/优化器类型（`HashGrid`↔`Frequency`） | instant-ngp 的 `configs/*.json`（改 `otype`） | 否 |
| 新增一种**不存在的** `otype`（新编码 `MyGrid`、新损失 `MyLoss`） | **tiny-cuda-nn**（实现子类 + 注册到工厂） | 是（两个仓库都要重编） |
| 用哈希编码 + 快速 MLP 从零搭一个新模型/新任务 | **tiny-cuda-nn 的 PyTorch 扩展**（脱离 instant-ngp） | 是 |

#### 4.3.2 核心流程

为什么边界是这样划的？因为 instant-ngp 只**消费**、不**实现**网络算子。流程上：

1. instant-ngp 的 `reset_network()` 从 JSON 取出 `loss_config`/`optimizer_config` 等四块。
2. 它调用 tiny-cuda-nn 的工厂函数 `create_loss(...)`、`create_optimizer(...)`、`create_encoding(...)`。
3. 这些工厂内部用一个 **`otype` → 具体子类** 的 `switch` 派发：字符串 `"Huber"` 就 new 一个 `HuberLoss`，`"Adam"` 就 new 一个 `Adam`。
4. 因此：新增一个 `otype`，必须去 tiny-cuda-nn 里实现该子类，并把它加进工厂的 `switch`——instant-ngp 这边一行代码都不用改（它会照常把 JSON 的 `otype` 字段透传过去），但必须重新编译。

反过来，任何「改值」「换已有类型」都只动 JSON，无需碰 tiny-cuda-nn。

#### 4.3.3 源码精读

README 在三处明确划定了这条边界。第一处是总纲——说明 instant-ngp 本身不实现网络，全靠 tiny-cuda-nn：

[README.md:L8](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L8) ——「In each case, we train and render a MLP with multiresolution hash input encoding using the **tiny-cuda-nn** framework.」

第二处是 FAQ，被问「如何在一个新任务上编辑/训练底层的哈希编码或神经网络」时，官方直接把你导向 tiny-cuda-nn：

[README.md:L279-L281](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L279-L281) ——「Q: How can I edit and train the underlying hash encoding or neural network on a new task? A: Use **tiny-cuda-nn**'s PyTorch extension.」

第三处是 Python 一节的收尾，同样指向 tiny-cuda-nn：

[README.md:L222](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L222) ——「If you would rather build new models from the hash encoding and fast neural networks, consider **tiny-cuda-nn**'s PyTorch extension.」

在代码侧，边界的「消费点」就在 `reset_network` 里这两行——instant-ngp 把配置交给工厂后就不闻不问了：

[src/testbed.cu:L4262-L4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263) —— `create_loss<network_precision_t>(loss_config)` 与 `create_optimizer<network_precision_t>(optimizer_config)`。这两个函数的实现在 `dependencies/tiny-cuda-nn/` 里：它们读 `config["otype"]` 字符串，`switch` 到对应子类。新增一种损失或优化器，改的就是 tiny-cuda-nn 里这个 `switch`，而不是 instant-ngp。

> 一句话证据：在本仓库里全局搜不到 `class Adam`、`class HuberLoss`、`class GridEncoding` 的实现——它们都在 tiny-cuda-nn。instant-ngp 只持有它们的 `shared_ptr`（u2-l1、u3-l1 讲过的五大模型对象）。

#### 4.3.4 代码实践

**实践目标**：针对几个具体需求，准确判断该改哪个仓库、改什么、是否重编。这是一个「源码阅读型 + 判定型」实践。

**操作步骤**：对下表每个需求，写下你的判定（instant-ngp / tiny-cuda-nn / 二者），再对照答案。

1. 需求 A：把 NeRF 的损失从 Huber 换成 `RelativeL2`。
2. 需求 B：新增一种名为 `MyLoss` 的损失函数。
3. 需求 C：让 SDF 的哈希编码 `base_resolution` 从 16 改成 8。
4. 需求 D：用哈希编码 + FullyFusedMLP 对一组时间序列做回归（全新任务）。

**需要观察的现象 / 预期结果**（即答案）：

- A → **instant-ngp**：`RelativeL2` 是 tiny-cuda-nn 已实现的 `otype`，只改 `configs/nerf/base.json` 的 `loss.otype` 即可，无需重编。
- B → **tiny-cuda-nn**：必须实现 `MyLoss` 子类并注册到 `create_loss` 的 `otype` switch；instant-ngp 不用改代码，但两个仓库都要重编。
- C → **instant-ngp**：只改 `configs/sdf/base.json` 的 `encoding.base_resolution`，无需重编。
- D → **tiny-cuda-nn（PyTorch 扩展）**：这是全新任务、脱离四种基元框架，按 README:L279-L281 的指引，应直接用 tiny-cuda-nn 的 PyTorch 扩展搭模型，而不是魔改 instant-ngp。

**待本地验证**：A/C 可立即在 JSON 上验证；B/D 涉及 tiny-cuda-nn 仓库，不在本仓库范围内。

#### 4.3.5 小练习与答案

**练习 1**：为什么 instant-ngp 里搜不到 `class FullyFusedMLP` 的实现，却能通过配置 `"otype": "FullyFusedMLP"` 用上它？

**参考答案**：因为 `FullyFusedMLP` 的实现在 tiny-cuda-nn。instant-ngp 的 `reset_network` 只把含 `otype` 的 `network_config` 透传给 tiny-cuda-nn 的工厂（如 [src/testbed.cu:L4262-L4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263) 的同类机制），由工厂的 `switch` 选出该子类。这是「应用层只消费、库层才实现」的典型体现。

**练习 2**：假设你在 tiny-cuda-nn 里新增了损失 `MyLoss` 并注册好，回到 instant-ngp 这边需要改 C++ 代码吗？需要重新编译 instant-ngp 吗？

**参考答案**：instant-ngp 这边**不用改 C++ 代码**——`reset_network` 已经会把任意 `otype` 字符串透传给 `create_loss`。但**需要重新编译 instant-ngp**（确切说是重新编译链接了 tiny-cuda-nn 的 `ngp` 静态库），因为新子类的符号要链进来。之后只要在 JSON 里写 `"otype": "MyLoss"` 即可使用。

## 5. 综合实践

把三个模块串起来，做一个完整的对照实验。这是本讲的综合任务，也直接呼应讲义规格里的实践要求。

**任务**：拷贝 `configs/nerf/base.json` 作为基准，修改 `n_levels` 与 `log2_hashmap_size` 两个参数，用 pyngp 脚本对比**参数量**与**训练损失曲线**；最后回答「若要改损失函数应去 tiny-cuda-nn 还是 instant-ngp」。

**步骤**：

1. **准备基准配置字典**。下面脚本直接内联一份与 `base.json` 编码/网络等价的最小配置（示例代码），免去读文件：

   ```python
   # 示例代码：综合对照实验
   import pyngp as ngp, copy, json

   BASE = {
       "loss":        {"otype": "Huber"},
       "optimizer":   {"otype": "Ema", "decay": 0.95, "nested": {
                          "otype": "ExponentialDecay", "decay_start": 20000,
                          "decay_interval": 10000, "decay_base": 0.33, "nested": {
                              "otype": "Adam", "learning_rate": 1e-2,
                              "beta1": 0.9, "beta2": 0.99, "epsilon": 1e-15, "l2_reg": 1e-6}}},
       "encoding":    {"otype": "HashGrid", "n_levels": 8,
                       "n_features_per_level": 4, "log2_hashmap_size": 19, "base_resolution": 16},
       "network":     {"otype": "FullyFusedMLP", "activation": "ReLU",
                       "output_activation": "None", "n_neurons": 64, "n_hidden_layers": 1},
       "dir_encoding":{"otype": "Composite", "nested": [
                          {"n_dims_to_encode": 3, "otype": "SphericalHarmonics", "degree": 4},
                          {"otype": "Identity"}]},
       "rgb_network": {"otype": "FullyFusedMLP", "activation": "ReLU",
                       "output_activation": "None", "n_neurons": 64, "n_hidden_layers": 2}
   }

   VARIANTS = {
       "base":      dict(n_levels=8,  log2=19),
       "more_lvls": dict(n_levels=16, log2=19),
       "small_tbl": dict(n_levels=8,  log2=15),
   }

   def run_variant(name, n_levels, log2, scene="data/nerf/fox", steps=1000):
       cfg = copy.deepcopy(BASE)
       cfg["encoding"]["n_levels"] = n_levels
       cfg["encoding"]["log2_hashmap_size"] = log2

       tb = ngp.Testbed(ngp.TestbedMode.Nerf)
       tb.load_training_data(scene)
       tb.shall_train = True
       tb.reload_network_from_json(cfg)            # 重建网络、从头训练

       curve = []
       while tb.training_step < steps:
           tb.frame()
           if tb.training_step % 50 == 0:
               curve.append((tb.training_step, tb.loss))
       return tb.n_params(), tb.n_encoding_params(), curve

   for name, v in VARIANTS.items():
       total, enc, curve = run_variant(name, v["n_levels"], v["log2"])
       print(f"{name:10s} n_levels={v['n_levels']:2d} log2={v['log2']:2d} "
             f"-> total={total} encoding={enc} final_loss={curve[-1][1]:.4f}")
   ```

2. **记录参数量**：脚本会打印每个变体的 `n_params()` 与 `n_encoding_params()`。预期 `more_lvls`（`n_levels` 翻倍）的编码参数显著上升；`small_tbl`（`log2` 从 19→15，哈希表小 16 倍）的编码参数显著下降。

3. **画损失曲线**：把三个 `curve` 用 matplotlib 画出 `training_step` vs `loss`。预期 `more_lvls` 因表达力更强而收敛到更低损失；`small_tbl` 因哈希表太小、碰撞严重而损失偏高。

4. **回答边界问题**：若要把损失从 Huber 改成一种**全新的**损失函数 `MyLoss`，应去 **tiny-cuda-nn** 修改（实现子类并注册到 `create_loss` 的 `otype` switch，见 [src/testbed.cu:L4262-L4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263)），instant-ngp 这边只需在 JSON 里写 `"otype": "MyLoss"` 并重新编译，不改 C++ 逻辑；若只是换成**已存在的**损失（如 `RelativeL2`），则连 tiny-cuda-nn 都不用动，只改 `configs/nerf/base.json` 的 `loss.otype` 即可。

**待本地验证**：脚本需在已编译 pyngp（`NGP_BUILD_WITH_PYTHON_BINDINGS=on`）的环境运行；精确参数量与曲线数值以本机为准。

## 6. 本讲小结

- instant-ngp 把网络结构完全外置到 `configs/*.json`，**绝大多数超参实验不需要改 C++、不需要重新编译**，配合 `parent` 继承只需写几行覆盖配置（`small.json`/`big.json` 即范例）。
- 配置加载链是 `find_network_config` → `load_network_config`（`parse` + `merge_parent_network_config`）→ 非 `.json` 触发 `reset_network()`；`reset_network` 对 `grid`/`permuto` 编码自动推导 `per_level_scale`，**显式正值优先**，并打印 `MultiLevelEncoding:` 行作为核对依据。
- pyngp 的 `reload_network_from_json(dict)` 可直接吃 Python 字典重建网络，配合 `n_params`/`n_encoding_params`/`loss`/`training_step`，构成可复现自动化对照实验的原子操作。
- instant-ngp（应用层）只通过工厂 `create_loss`/`create_optimizer`/`create_encoding`/`create_network` **消费**算子，所有算子实现在 **tiny-cuda-nn（库层）**——这是「该改哪里」的总边界。
- 判定规则：改值/换已有 `otype` → 只动 instant-ngp 的 JSON、免重编；新增 `otype`（新编码/损失/优化器）→ 改 tiny-cuda-nn 并重编；全新任务/新模型 → 用 tiny-cuda-nn 的 PyTorch 扩展（README 明确指引）。

## 7. 下一步学习建议

本讲是手册终篇，不再有「下一讲」。建议你按以下方向继续深入：

- **动手做一次端到端实验**：用本讲综合实践的脚本，对自己的数据集（先用 `scripts/colmap2nerf.py` 生成 `transforms.json`，见 u7-l3）做一次 `n_levels`/`log2_hashmap_size` 网格搜索，体会配置调参的真实手感。
- **跨进 tiny-cuda-nn 仓库**：阅读 `dependencies/tiny-cuda-nn/` 里 `create_loss`/`create_encoding` 的 `otype` switch，看一个新算子如何注册；这是从「instant-ngp 用户」升级为「tiny-cuda-nn 贡献者」的门槛。
- **尝试 PyTorch 扩展**：按 README:L222 / L279-281 的指引，用 tiny-cuda-nn 的 PyTorch 扩展搭一个不属于四种基元的小模型（如 1D 信号回归），彻底打通「应用层 ↔ 库层」的认知。
- **回头精读中枢**：如果还想继续啃 instant-ngp 本体，重读 u2-l1/u2-l2 的 Testbed 与 `frame()` 主循环，把八单元串成一张完整的调用链地图。
