# 命令行覆盖与 dotlist 合并：官方脚本入口的秘密

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `--train_module.optim.lr=6e-3` 这样一条命令行参数，从字符串到配置对象字段的完整旅程（`_clean_opt` → `_set_nested` → `from_dict`）。
2. 读懂 `src/olmo_core/script_utils.py` 提供的官方脚本三件套：`get_cli_parser()`（统一命令行骨架）、`ExperimentConfig`（实验顶层配置）、`main()`（从命令行到 `trainer.fit()` 的入口流程）。
3. 独立写出一个接受 dotlist 覆盖的自定义入口脚本：从 YAML 加载 `ExperimentConfig`，应用命令行覆盖，打印最终配置并存回 YAML。

本讲承接 u2-l1（`Config` 基类与序列化）和 u2-l2（`Registrable` 多态字段）。那两讲回答了「配置是什么、怎么存取」；本讲回答「配置怎么被命令行动态修改，以及官方脚本如何把这一切串成可复现的实验入口」。

## 2. 前置知识

### 2.1 argparse 的 `parse_known_args`

Python 标准库 `argparse` 通常用 `parse_args()`：遇到未定义的参数会直接报错退出。而 `parse_known_args()` 更宽容——它把参数分成两堆：

- **已知选项**：parser 里用 `add_argument` 注册过的，如 `--save-folder`，解析进 `Namespace` 对象；
- **剩余参数**（unknown/overrides）：没注册的，原样作为字符串列表返回。

OLMo-core 正是利用这个分流：**少量稳定的通用选项注册成正式参数，无穷无尽的配置覆盖项全部留给 dotlist**。这样不用为每个配置字段写一行 `add_argument`。

### 2.2 dotlist 回顾

dotlist 就是「用点号表示嵌套路径的键值对列表」，例如：

```python
["--train_module.optim.lr=6e-3", "--data_loader.num_workers=4"]
```

u2-l1 已经讲过 `Config.merge(dotlist)` 会返回一个**新对象**，并且值按 YAML 规则解析（`6e-3` 变 float、`true` 变 bool）。本讲深入它的内部实现。

### 2.3 一个必须建立的直觉：覆盖发生在「字典世界」，不是「对象世界」

初学者常以为覆盖是 `setattr(config.train_module.optim, "lr", 0.006)` 这样在对象上直接改。实际不是：`merge()` 先把整个配置树**降级成普通字典**，在字典里下钻改值，再**从字典重建**出全新的配置对象（内部走的正是 u2-l1 讲过的 `from_dict` + `_CLASS_` 还原、u2-l2 讲过的多态解码）。这个「降级—修改—重建」的设计让多态字段（如 `optim: OptimConfig` 实际是 `SkipStepAdamWConfig`）在覆盖后不会退化成基类。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `src/olmo_core/config.py` | `Config` 基类（u2-l1/u2-l2 已讲） | `merge()`、`_set_nested()`、`_clean_opt()`、`from_dict()` 的 `overrides` 参数 |
| `src/olmo_core/script_utils.py` | 官方脚本的入口工具箱 | `ExperimentConfig`、`get_cli_parser()`、`_parse_args()`、`main()` |
| `src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py` | OLMo-2 32B 官方预训练脚本 | `build_config()` 如何消费 `opts` 并以 `.merge(overrides)` 收尾 |
| `src/examples/llm/train.py` | 自包含训练示例（u1-l4 已讲） | 自定义 `ExperimentConfig` 与同款 merge 收尾，作对照 |
| `src/olmo_core/train/callbacks/config_saver.py` | 训练中落盘配置的回调 | 最终配置如何被记录进每个 checkpoint |
| `src/test/config_test.py` | `Config` 的单元测试 | `merge` 的 strict/prefix 行为的权威断言 |
| `src/scripts/official/README.md` | 官方脚本使用说明 | 对覆盖用法的一手文档 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Config.merge() 的 dotlist 流水线**——覆盖机制的引擎；
2. **get_cli_parser()**——命令行如何把「正式选项」和「覆盖项」分流；
3. **ExperimentConfig 与 main()**——官方脚本把二者组装成完整入口。

### 4.1 模块一：Config.merge()——dotlist 覆盖的三步流水线

#### 4.1.1 概念说明

训练大模型时，同一份脚本要服务无数变体实验：换个学习率、换个数据根目录、关掉某个回调。如果每个字段都写成命令行参数，脚本会膨胀到无法维护。OLMo-core 的解法是：**配置结构本身是树（嵌套 Config），覆盖项就是树上的路径**。`merge()` 接收任意多条路径=值，一次性作用到整棵树上，返回新配置。

它要解决三个具体问题：

1. **字符串到 Python 值的解析**：命令行里一切都是字符串，`6e-3` 要变成 `0.006`；
2. **深层嵌套的定位**：`train_module.optim.lr` 要穿过三层结构找到目标；
3. **多态身份的保留**：改完之后 `optim` 仍然是原来那个注册子类，不会丢 `type` 信息（u2-l2）。

#### 4.1.2 核心流程

```text
输入: config 对象 + dotlist（如 ["--train_module.optim.lr=6e-3"]）

第 1 步  _clean_opts(dotlist)
         每项做 _clean_opt:
           "无等号"        → 值默认为 "true"（即布尔开关）
           "去壳与归一化"   → 去掉前导 '-'，把 '-' 全部替换为 '_'（兼容命令行风格）
           "解析值"        → yaml.safe_load(值字符串) → 得到 int/float/bool/list/str...

第 2 步  merged_data = config.as_dict(include_class_name=True, include_registered_name=True)
         整棵配置树降级为普通字典，每个节点带 _CLASS_（和注册名 type）线索

第 3 步  逐条 _set_nested(merged_data, key, value)
         按 '.' 逐层下钻（字典按 key，列表按下标），在叶子处写入新值

第 4 步  return config.from_dict(merged_data)
         走 u2-l1 讲过的解码路径：按 _CLASS_ 动态 import 还原确切子类，
         重建出全新的配置对象
```

第 2→4 步合起来就是「字典世界绕一圈」。代价是每次 merge 都重建整棵树；收益是覆盖逻辑与序列化逻辑**共用同一套基础设施**（`as_dict`/`from_dict`），且多态天然安全。

#### 4.1.3 源码精读

**值清洗：`_clean_opt`**——单条覆盖项从字符串到 `(键, Python值)` 的全部逻辑：

[config.py:L348-L362](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L348-L362)

```python
def _clean_opts(opts: Sequence[str]) -> list[tuple[str, Any]]:
    return [_clean_opt(s) for s in opts]

def _clean_opt(arg: str) -> tuple[str, Any]:
    if "=" not in arg:
        name, val = arg, "true"
    else:
        name, val = arg.split("=", 1)
    name = name.strip(" -").replace("-", "_")
    if not val or val.isspace():
        val = ""
    else:
        val = yaml.safe_load(val)
    return (name, val)
```

这段代码做了四件事：无 `=` 时值默认为 `"true"`（所以 `--trainer.save_overwrite` 单独出现等价于 `=true`）；`strip(" -")` 吃掉参数名前导的 `--`（argparse 的 overrides 列表里参数仍带着 `--` 前缀）；`replace("-", "_")` 让 `--data-loader.prefetch-factor=4` 这种连字符写法也能命中字段；最后 `yaml.safe_load(val)` 把值字符串解析成真正的 Python 类型——`6e-3` → `0.006`，`[1,2]` → 列表，`null` → `None`。

**字典下钻：`_set_nested`**——递归定位并写入：

[config.py:L330-L345](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L330-L345)

```python
def _set_nested(data: Any, key: str, value: Any):
    if "." in key:
        key, child_keys = key.split(".", 1)
        if isinstance(data, dict):
            _set_nested(data[key], child_keys, value)
        elif isinstance(data, list):
            _set_nested(data[int(key)], child_keys, value)
        ...
    else:
        if isinstance(data, dict):
            data[key] = value
        elif isinstance(data, list):
            data[int(key)] = value
        ...
```

注意它同时支持字典键和**列表下标**——你可以覆盖 `trainer.callbacks.checkpointer` 这类字段，也可以覆盖形如 `model.block_overrides.0.attention.dropout_p` 的列表元素（数字键会被当作下标）。键不存在于字典时 `data[key]` 会抛 `KeyError`，这就是「拼错字段名立刻报错」的来源。

**主体：`Config.merge`**：

[config.py:L197-L225](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L197-L225)

```python
def merge(self, dotlist: List[str], prefix: Optional[str] = None, strict: bool = True) -> Self:
    overrides = _clean_opts(dotlist)
    if prefix is not None:
        overrides = [
            (k.replace(f"{prefix}.", "", 1), v)
            for k, v in overrides
            if k.startswith(f"{prefix}.")
        ]

    if not strict:
        field_names = set(f.name for f in dataclasses.fields(self))
        overrides = [
            (k, v)
            for k, v in overrides
            if any([k == name or k.startswith(f"{name}.") for name in field_names])
        ]

    merged_data = self.as_dict(include_class_name=True, include_registered_name=True)
    for key, value in overrides:
        _set_nested(merged_data, key, value)
    return self.from_dict(merged_data)
```

四个要点：

1. `prefix` 参数：只采纳以 `prefix.` 开头的覆盖项并剥掉前缀。适合「一堆混合参数里只取属于我的那份」的场景；
2. `strict=False`：默默丢弃与本类字段不匹配的覆盖项（用于宽松模式）；默认 `strict=True` 时，拼错的路径会在 `_set_nested` 处抛错——这是保护，不是麻烦；
3. `as_dict(include_class_name=True, include_registered_name=True)`：降级成字典时保留 `_CLASS_` 与 `type` 双线索（u2-l2 讲过的多态序列化格式），所以第 4 步 `from_dict` 能原样还原每个子类；
4. 返回值是 `from_dict(merged_data)` 构建的**新对象**，原配置不被修改（不可变风格，方便对比与复用）。

**官方脚本中的收尾一行**——OLMo-2 32B 官方脚本 `build_config()` 的最后：

[OLMo-2-0325-32B-train.py:L229-L235](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L229-L235)

```python
    return ExperimentConfig(
        model=model_config,
        dataset=dataset_config,
        data_loader=data_loader_config,
        train_module=train_module_config,
        trainer=trainer_config,
    ).merge(overrides)
```

先用 Python 代码搭出完整的默认配方（模型 32B、优化器 `lr=6e-4`、数据混合 OLMoE_mix_0824……），再用 `.merge(overrides)` 把命令行覆盖打上去——**代码定义基线，命令行做微调**，这就是官方脚本可复现又可定制的秘密。examples 里的 [train.py:L265-L268](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L265-L268) 是完全同款收尾。

**同一引擎的另一个入口：`from_dict` 的 `overrides` 参数**：

[config.py:L297-L299](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L297-L299)

```python
        if overrides:
            for key, value in _clean_opts(overrides):
                _set_nested(data, key, value)
```

`from_yaml(path, overrides=[...])` / `from_json(path, overrides=[...])`（[config.py:L307-L327](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L307-L327)）会在解码**之前**把覆盖打进刚读出的字典——效果与先 `from_yaml` 再 `merge` 等价，但只解码一次。本讲主实践会用到这个更紧凑的入口。

**测试中的权威行为**——三个断言值得记住：

- [config_test.py:L38-L41](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L38-L41)：`from_dict(data, overrides=["bar.x=0"])` 与 `merge(["bar.x=-1"])` 都能命中嵌套字段；
- [config_test.py:L83-L85](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L83-L85)：`strict=False` 时 `--baz.booz=0` 这种无关项被静默忽略；
- [config_test.py:L99-L101](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L99-L101)：`prefix="foo"` 时 `--foo.bar.x=0` 剥前缀后命中 `bar.x`。

#### 4.1.4 代码实践：亲手验证覆盖语义

**实践目标**：用一个最小脚本验证 `_clean_opt` 的三条规则（YAML 值解析、无 `=` 默认 true、连字符转下划线）和 `strict` 的报错/忽略行为。

**操作步骤**（以下为示例代码，保存为 `merge_playground.py`，可在仓库外任意目录用 `python merge_playground.py` 运行；需已按 u1-l2 完成开发安装）：

```python
# 示例代码：merge_playground.py
from dataclasses import dataclass
from olmo_core.config import Config

@dataclass
class OptimCfg(Config):
    lr: float = 1e-3
    betas: tuple = (0.9, 0.95)

@dataclass
class TrainModuleCfg(Config):
    optim: OptimCfg = OptimCfg()
    compile_model: bool = False

tm = TrainModuleCfg()

# 1) YAML 值解析 + 连字符归一化：'--optim.lr' 命中, 6e-3 变 float
tm1 = tm.merge(["--optim.lr=6e-3"])
print("lr =", tm1.optim.lr, type(tm1.optim.lr))        # 期待 0.006 <class 'float'>

# 2) 无 '=' 默认 true（注意连字符写法 compile-model 也能命中 compile_model）
tm2 = tm.merge(["--compile-model"])
print("compile_model =", tm2.compile_model)            # 期待 True

# 3) strict 行为：先看默认（strict=True）报错, 再看 strict=False 忽略
try:
    tm.merge(["--optim.typo_field=1"])                 # 不存在的路径
    print("no error?")
except Exception as e:
    print("strict=True 报错:", type(e).__name__, e)

tm3 = tm.merge(["--optim.typo_field=1", "--optim.lr=2e-3"], strict=False)
print("strict=False 忽略 typo, lr =", tm3.optim.lr)    # 期待 0.002

# 4) 不可变风格：原对象未被修改
print("原对象 lr 仍是 =", tm.optim.lr)                  # 期待 0.001
```

**需要观察的现象**：值 `6e-3` 被解析为 float 而非字符串；`--compile-model` 无需 `=true`；strict 模式下错误路径抛出的异常类型与信息；merge 前后原对象保持不变。

**预期结果**：按源码逻辑应依次打印 `0.006 float`、`True`、`strict=True 报错 KeyError ...`（`_set_nested` 在 `data[key]` 处触发；具体异常类型与消息**待本地验证**——若 `from_dict` 解码阶段先于字典下钻抛出 `OLMoConfigurationError`，以其为准）、`0.002`、`0.001`。

#### 4.1.5 小练习与答案

**练习 1**：命令行给 `--train_module.optim.betas=[0.9,0.98]`，最终 `betas` 字段是什么类型和值？为什么不会是字符串 `"[0.9,0.98]"`？

**答案**：是 Python 列表 `[0.9, 0.98]`。因为 `_clean_opt` 用 `yaml.safe_load` 解析值字符串（config.py:L361），YAML 语法中 `[a, b]` 就是流式列表。解码回对象时由 `dataclass_extensions.decode` 按字段类型还原成 tuple。

**练习 2**：为什么 `merge()` 选择「转字典改完再 `from_dict` 重建」，而不是直接在对象上 `setattr`？说出至少两个理由。

**答案**：① 多态安全——重建走 `_CLASS_`/`type` 线索，注册子类（如 `SkipStepAdamWConfig`）不退化；② 复用序列化基础设施——下钻、类型解析与 YAML 加载共用一套代码，无需为对象图另写遍历器；③ 列表/字典字段可统一定位（`_set_nested` 同时处理 dict key 与 list 下标）；④ 不可变风格——返回新对象，原始配置可安全复用与对比。

**练习 3**：`merge(["--foo.bar.x=0"], prefix="foo")` 与 `merge(["--bar.x=0"])` 结果有何异同？

**答案**：对该配置对象的效果相同（都把 `bar.x` 置 0）；区别在于前者会**过滤掉**所有不以 `foo.` 开头的覆盖项，适合从混合参数列表里只摘取本层关心的部分。

### 4.2 模块二：get_cli_parser()——官方脚本统一的命令行骨架

#### 4.2.1 概念说明

官方脚本目录下所有训练脚本共享同一个命令行界面（见 [official/README.md:L7-L10](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/README.md#L7-L10)）：少数几个正式选项 + 任意多个 dotlist 覆盖。`get_cli_parser()` 就是这个统一界面的定义处。它解决的问题是：

1. **免重复**：几十个官方脚本不必各自手写 argparse；
2. **可发现**：`--help` 一次列出全部通用选项；
3. **可扩展**：返回标准 `argparse.ArgumentParser`，调用方还能继续 `add_argument` 追加自己的选项（examples/llm/train.py 就是先自建 parser 再 `parse_known_args` 的同款变体，见 [train.py:L311](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L311)）。

#### 4.2.2 核心流程

```text
get_cli_parser()
   │  注册 7 个通用选项（name/sequence-length/data-root/save-folder/work-dir/dry-run/train-single）
   ▼
_parse_args(parser)
   │  parser.parse_known_args()  ──►  opts（Namespace，已知选项）
   │                              ──►  overrides（str 列表，全部未知参数，保留 -- 前缀）
   │  若未给 --work-dir: 按 save-folder 是否为 URL 推断默认值
   ▼
(opts, overrides) 交给上层（main() 或你的自定义入口）
```

#### 4.2.3 源码精读

**parser 定义**：

[script_utils.py:L39-L91](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L39-L91)

```python
def get_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=sys.argv[0],
        usage=f"python {sys.argv[0]} [OPTIONS...] [CONFIG_OVERRIDES...]",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
```

usage 字符串明确写出了「`[OPTIONS...] [CONFIG_OVERRIDES...]`」的两段式设计。七个选项逐一过一遍：

| 选项 | 必填 | 用途 |
|---|---|---|
| `--name` | 否 | 运行名称，供日志（WandB/Comet）使用 |
| `--sequence-length` | 否 | 训练/评估序列长度；缺省时用脚本自己的默认值（如 32B 脚本的 4096） |
| `--data-root` | 否 | 数据文件根目录/URL，默认公网 `https://olmo-data.org` |
| `--save-folder` | **是** | checkpoint 保存目录（本地共享盘或 `s3://`/`gs://`），所有 rank 必须可达 |
| `--work-dir` | 否 | 数据预处理工作目录，缺省时推断 |
| `--dry-run` | 否 | 打印最终配置后立即退出 |
| `--train-single` | 否 | 单 rank 调试模式 |

其中 `--save-folder` 的定义在 [script_utils.py:L66-L74](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L66-L74)（`required=True`），`--dry-run` 在 [script_utils.py:L81-L85](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L81-L85)。

**分流与 work-dir 推断：`_parse_args`**：

[script_utils.py:L94-L104](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L94-L104)

```python
def _parse_args(parser=None) -> Tuple[argparse.Namespace, List[str]]:
    parser = parser if parser is not None else get_cli_parser()
    opts, overrides = parser.parse_known_args()
    if opts.work_dir is None:
        if is_url(opts.save_folder):
            opts.work_dir = "/tmp/olmo-core/dataset-cache"
        else:
            opts.work_dir = opts.save_folder
    return opts, overrides
```

`parse_known_args()` 一行完成「正式选项 / 覆盖项」分流；随后一个小便利：保存目录是远端 URL 时，本地数据缓存自动落到 `/tmp/olmo-core/dataset-cache`，否则直接复用保存目录。注意 overrides 里的字符串**原样带 `--` 前缀**——这正是 `_clean_opt` 里 `strip(" -")` 存在的原因。

#### 4.2.4 代码实践：零成本体验官方 CLI

**实践目标**：不启动任何训练，用 `--help` 与 `--dry-run` 体验统一 CLI 的两段式设计。

**操作步骤**（在仓库根目录执行；`--dry-run` 只构造并打印配置，不初始化分布式后端、不读数据文件——dataset 配置在 `from_data_mix` 里只记录混合名，路径延迟到真正 `build()` 才解析）：

```bash
# 1) 查看统一 CLI 的全部选项
python src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py --help

# 2) dry-run 打印完整配置（save-folder 是 required，即使 dry-run 也必须给）
python src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py \
  --save-folder=/tmp/dry \
  --train_module.optim.lr=6e-3 \
  --data_loader.num_workers=2 \
  --dry-run
```

**需要观察的现象**：`--help` 输出的 usage 行是否是 `[OPTIONS...] [CONFIG_OVERRIDES...]` 两段式；dry-run 打印的配置树里 `train_module.optim.lr` 是否为 `0.006`、`data_loader.num_workers` 是否为 `2`；不给 `--save-folder` 时 argparse 是否直接报错退出。

**预期结果**：`--help` 列出上表 7 个选项；dry-run 打印的配置中两处覆盖均已生效（`lr=0.006` 来源于 `_clean_opt` 的 `yaml.safe_load("6e-3")`）。32B 模型配置的构造本身不需要 GPU。实际终端输出**待本地验证**；若 dry-run 阶段有额外的环境准备日志，对照 `main()` 源码 L112-L113 理解。

#### 4.2.5 小练习与答案

**练习 1**：官方脚本没有为 `optim.lr` 写任何 `add_argument`，为什么 `--train_module.optim.lr=6e-3` 不会被 argparse 拒绝？

**答案**：因为脚本（经 `main()` → `_parse_args()`）用的是 `parse_known_args()` 而非 `parse_args()`。未注册的参数不报错，而是作为字符串进入 overrides 列表，最终交给 `ExperimentConfig(...).merge(overrides)` 消化。

**练习 2**：`--save-folder` 为什么设计成 `required=True`，而 `--work-dir` 不是？

**答案**：checkpoint 保存位置是每次实验必须明确决策的安全关键信息（写错目录可能覆盖重要数据），且没有合理默认值；而 `--work-dir` 可以从 `--save-folder` 推断（URL 时用 `/tmp/olmo-core/dataset-cache`，本地路径时直接复用，见 `_parse_args` L99-L103），所以允许缺省。

**练习 3**：如果想在官方脚本上额外加一个 `--my-flag` 选项，`get_cli_parser()` 返回的 parser 支持吗？

**答案**：支持。它返回的就是普通 `argparse.ArgumentParser`，调用方可以继续 `parser = get_cli_parser(); parser.add_argument("--my-flag", ...)` 再 `parse_known_args()`。examples/llm/train.py 展示的正是「自定义 parser + 同款分流」的自建变体。

### 4.3 模块三：ExperimentConfig 与 main()——从命令行到 trainer.fit()

#### 4.3.1 概念说明

u1-l4 已经用 examples/llm/train.py 建立了「一次实验 = 一份顶层 ExperimentConfig」的认知。`script_utils.py` 把这个模式**标准化**了：

- `ExperimentConfig`：库自带的顶层配置 dataclass，五个核心字段正好对应五类积木（模型、数据集、数据加载、训练模块、训练器）；
- `main(config_builder)`：通用的「跑一个实验」函数——解析参数、调用你提供的 `config_builder(opts, overrides)` 得到配置、然后按固定顺序完成环境准备、组件构建、checkpoint 恢复与训练。

官方脚本因此可以把全部篇幅用于**定义配方**（`build_config`），一行 `main(build_config)` 就获得完整的可执行入口。

#### 4.3.2 核心流程

`main()` 的执行序列（对照源码）：

```text
main(config_builder)
 1. _parse_args()                      → (opts, overrides)
 2. 若 --dry-run: prepare_cli_environment()（只装日志/异常钩子等，不碰分布式）
 3. config = config_builder(opts, overrides)     ← 脚本作者提供的配方函数
 4. 若 --dry-run: rich.print(config); return     ← 到此为止，零 GPU 零数据
 5. 若 --train-single: 警告并禁用 dp_config / tp_config（单进程跑不了数据/张量并行）
 6. prepare_training_environment(...)            ← 初始化分布式进程组（torchrun 拉起）
 7. seed_all(config.init_seed)                   ← 全局随机种子
 8. 依序 build：model → train_module → dataset → data_loader → trainer
 9. 把 config.as_config_dict() 塞给 ConfigSaverCallback（随 checkpoint 落盘）
10. 恢复 checkpoint（save 目录优先，否则 load_path）
11. trainer.fit() → teardown_training_environment()
```

#### 4.3.3 源码精读

**ExperimentConfig：五块积木的集装箱**：

[script_utils.py:L28-L36](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L28-L36)

```python
@dataclass
class ExperimentConfig(Config):
    model: TransformerConfig
    dataset: NumpyDatasetConfig
    data_loader: NumpyDataLoaderConfig
    train_module: TransformerTrainModuleConfig
    trainer: TrainerConfig
    init_seed: int = 12536
    load_path: Optional[str] = None
```

五个字段覆盖实验全貌（模型结构、数据、加载方式、优化与并行、训练循环与回调），另有两个实验级选项：初始化种子和「save 目录无 checkpoint 时的加载来源」（常用于从预训练权重微调）。注意字段的类型标注都是**基类**——配合 u2-l2 的多态机制，`dataset` 实际可以是 `NumpyFSLDatasetConfig` 等任何注册子类，dotlist 覆盖也能精确命中子类自己的字段。examples 的 [train.py:L85-L106](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L85-L106) 是它的自包含翻版（多了 `load_trainer_state` 字段）。

**main()：dry-run 与 train-single 两个短路分支**：

[script_utils.py:L107-L133](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L107-L133)

```python
def main(config_builder, parser=None) -> None:
    opts, overrides = _parse_args(parser)
    if opts.dry_run:
        prepare_cli_environment()

    config = config_builder(opts, overrides)

    if opts.dry_run:
        rich.print(config)
        return

    if opts.train_single:
        if (dp_config := getattr(config.train_module, "dp_config", None)) is not None:
            log.warning("'dp_config' is set to %s, ... Disabling.", dp_config)
            config.train_module.dp_config = None
        if (tp_config := getattr(config.train_module, "tp_config", None)) is not None:
            ...
```

dry-run 在**任何分布式初始化之前**返回，所以它能在纯 CPU、无数据的环境里验证配置；`train_single` 分支则用 `getattr(..., None)` 防御式探测并关闭并行配置——单进程下数据/张量并行不仅无益而且无法初始化。

**环境准备与组件构建**：

[script_utils.py:L135-L149](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L135-L149)

```python
    prepare_training_environment(shared_filesystem=not is_url(opts.save_folder), backend=backend)
    seed_all(config.init_seed)

    # Build components.
    model = config.model.build(init_device="meta")
    train_module = config.train_module.build(model)
    dataset = config.dataset.build()
    data_loader = config.data_loader.build(dataset, dp_process_group=train_module.dp_process_group)
    trainer = config.trainer.build(train_module, data_loader)
```

构建顺序与 u1-l4 讲过的依赖链一致：模型先在 meta 设备占位（不真正分配显存），由 train_module 的并行策略落位；data_loader 需要 train_module 的数据并行进程组；trainer 最后把 train_module 和 data_loader 编排起来。有 CUDA 时 backend 指定为 `"cpu:gloo,cuda:nccl"`（L135-L138）。

**最终配置落盘：ConfigSaverCallback**：

[script_utils.py:L151-L155](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L151-L155) 把 `config.as_config_dict()`（u2-l1 讲过的官方存档格式）塞给回调：

```python
    for callback in trainer.callbacks.values():
        if isinstance(callback, ConfigSaverCallback):
            callback.config = config.as_config_dict()
            break
```

回调侧的 setter 会顺带把配置传播给 WandB/Comet/Beaker 回调（[config_saver.py:L44-L55](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/callbacks/config_saver.py#L44-L55)），而每次保存 checkpoint 时把配置写成 `config.json`（[config_saver.py:L57-L64](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/callbacks/config_saver.py#L57-L64)）。**命令行上的每一条覆盖，最终都被固化进 checkpoint**——这就是「实验可复现」的最后一环。

**收尾：恢复与训练**：

[script_utils.py:L157-L169](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L157-L169)：save 目录里没有 checkpoint 且配置了 `load_path` 时，从 `load_path` 加载模型权重（`load_trainer_state=False`，只取权重不续训练状态）；随后 `trainer.fit()` 进入 u6-l1 将剖析的训练循环，最后拆除分布式环境。

**opts 如何渗入配方**：`config_builder` 的签名是 `(opts, overrides)`，两类输入以不同方式进入配置——`opts` 由配方作者显式读取（如 32B 脚本中 [L44](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L44) 读 `opts.sequence_length`、[L54](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L54) 读 `opts.data_root` 拼数据路径），`overrides` 则在 [L235](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L235) 统一交给 `.merge()`。脚本入口只有两行（[L238-L239](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L238-L239)）：

```python
if __name__ == "__main__":
    main(build_config)
```

#### 4.3.4 代码实践：写出你自己的最小入口脚本（本讲主实践）

**实践目标**：基于 `script_utils.get_cli_parser()` 写一个最小入口脚本 `run_exp.py`：从 YAML 加载 `ExperimentConfig`，接受命令行 dotlist 覆盖，打印最终配置并保存为 YAML。

**操作步骤**：

第一步，生成一份基线配置 YAML。以下为示例代码（保存为 `make_base_config.py`，在仓库根目录运行 `python make_base_config.py`；只构造配置不 `build()`，因此不会读取任何数据文件）：

```python
# 示例代码：make_base_config.py —— 生成基线 ExperimentConfig 并存为 YAML
import yaml
from olmo_core.data import NumpyDataLoaderConfig, NumpyFSLDatasetConfig, TokenizerConfig
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.optim import AdamWConfig, CosWithWarmup
from olmo_core.script_utils import ExperimentConfig
from olmo_core.train import TrainerConfig
from olmo_core.train.train_module import TransformerTrainModuleConfig

tok = TokenizerConfig.gpt2()                      # vocab_size=50257, 纯本地构造
config = ExperimentConfig(
    model=TransformerConfig.olmo2_14M(vocab_size=tok.padded_vocab_size()),  # → 50304
    dataset=NumpyFSLDatasetConfig(
        paths=["/tmp/fake/c4.npy"],               # 占位路径：不 build 就不会被读取
        sequence_length=1024,
        tokenizer=tok,
        work_dir="/tmp/dataset-cache",
    ),
    data_loader=NumpyDataLoaderConfig(global_batch_size=8192, seed=0, num_workers=2),
    train_module=TransformerTrainModuleConfig(
        rank_microbatch_size=2048,                # 均以 token 计（u1-l4）
        max_sequence_length=1024,
        optim=AdamWConfig(lr=1e-3),
        scheduler=CosWithWarmup(warmup_steps=10),
    ),
    trainer=TrainerConfig(save_folder="/tmp/my-exp"),
)
# u2-l1 讲过：Config 没有 save()，自行把 as_config_dict() 结果写盘。
# as_config_dict(json_safe=True) 已把 tuple 转成 list，yaml.safe_dump 不会报错。
with open("base_config.yaml", "w") as f:
    yaml.safe_dump(config.as_config_dict(), f, sort_keys=False)
print("written to base_config.yaml")
```

第二步，写入口脚本。以下为示例代码（保存为 `run_exp.py`）：

```python
# 示例代码：run_exp.py —— 最小实验入口：YAML + dotlist 覆盖 → 打印 + 存档
import argparse
import yaml
import rich

from olmo_core.script_utils import get_cli_parser
from olmo_core.script_utils import ExperimentConfig

def main() -> None:
    # 在统一骨架上追加自己的参数：配置文件路径
    parser = get_cli_parser()
    parser.add_argument("config", type=str, help="Path to base experiment config (YAML)")
    opts, overrides = parser.parse_known_args()   # 两段式分流（4.2）

    # 从 YAML 加载并在解码前应用 dotlist 覆盖（等价于 from_yaml 后再 merge）
    config = ExperimentConfig.from_yaml(opts.config, overrides=overrides)

    rich.print(config)                            # 打印最终配置（与官方 --dry-run 同款展示）

    # 存回 YAML：落到 --save-folder（get_cli_parser 的 required 选项）
    out_path = f"{opts.save_folder.rstrip('/')}/final_config.yaml"
    with open(out_path, "w") as f:
        yaml.safe_dump(config.as_config_dict(), f, sort_keys=False)

    # 复现性自检：存档再读回，应与内存对象相等（u2-l1 的 round-trip 结论）
    reloaded = ExperimentConfig.from_yaml(out_path)
    print("round-trip equal:", reloaded == config)
    print("saved to", out_path)

if __name__ == "__main__":
    main()
```

第三步，运行并做覆盖实验（`--save-folder` 是 required，即使本脚本并不训练也要给）：

```bash
mkdir -p /tmp/my-exp
python run_exp.py base_config.yaml --save-folder=/tmp/my-exp \
  --train_module.optim.lr=6e-3 \
  --data_loader.num_workers=1 \
  --init_seed=42
```

**需要观察的现象**：终端打印的配置树中 `train_module.optim.lr` 是否变为 `0.006`、`data_loader.num_workers` 是否为 `1`、`init_seed` 是否为 `42`；`/tmp/my-exp/final_config.yaml` 里每个配置节点是否都带 `_CLASS_`（以及多态节点的 `type`，如 `optim` 节点）；`round-trip equal` 是否为 `True`；`overrides` 中带 `--` 前缀的字符串为何最终能命中下划线命名的字段。

**预期结果**：三处覆盖全部生效（依据 `from_dict` L297-L299 + `_clean_opt` 的解析规则）；`final_config.yaml` 是含 `_CLASS_`/`type` 双线索的完整配置（承接 u2-l2 的多态序列化格式）；round-trip 相等输出 `True`。若 `NumpyFSLDatasetConfig` 或 `TransformerTrainModuleConfig` 在构造阶段对字段有额外校验导致第一步报错，按报错信息补齐缺省字段后重试（各字段默认值见其类定义，`dp_config` 等并行配置均有默认 `None`，可省略）。完整运行输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`main()` 里为什么 `--dry-run` 分支调用的是 `prepare_cli_environment()`（只装日志、异常钩子）而不是 `prepare_training_environment()`（初始化进程组）？

**答案**：dry-run 的目的就是在「零 GPU、零数据、单进程」下预览最终配置。`prepare_training_environment()` 会初始化分布式后端，需要 torchrun 拉起多进程环境；dry-run 在它之前 `return`（L117-L119），使配置检查完全脱离集群。

**练习 2**：官方脚本的 `build_config` 里已经把 `lr` 硬编码为 `6e-4`（32B 脚本 L70），命令行 `--train_module.optim.lr=6e-3` 为什么能覆盖它？

**答案**：`build_config` 返回的是 `ExperimentConfig(...).merge(overrides)`（L229-L235）。merge 发生在 Python 配置构造**之后**，命令行的覆盖值在字典世界里替换掉硬编码值，最终 `from_dict` 重建出的配置携带新值 `0.006`。即「代码定基线、命令行做微调」。

**练习 3**：训练完成后，你在命令行里做过的覆盖参数还能找回来吗？去哪里找？

**答案**：能。`main()` 把 `config.as_config_dict()`（已含覆盖结果）设到 `ConfigSaverCallback`（script_utils.py L151-L155），该回调在每个 checkpoint 目录写 `config.json`（config_saver.py L57-L64），并同步给 WandB/Comet 回调。查任意 checkpoint 的 `config.json` 即可还原当时全部生效的配置。

## 5. 综合实践

**任务：给一条同事发来的训练命令做「预检」，不启动训练确认它最终会以什么配置运行。**

假设同事发来：

```bash
torchrun --nproc-per-node=8 src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py \
  --save-folder=/tmp/exp-a \
  --sequence-length=2048 \
  --train_module.optim.lr=4e-4 \
  --data_loader.num_workers=2 \
  --dry-run
```

请完成三件事：

1. **静态追踪**（纸面作业）：对照 [script_utils.py:L94-L104](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/script_utils.py#L94-L104) 与 [OLMo-2-0325-32B-train.py:L43-L58](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L43-L58)，写出这条命令里每个参数的归宿：哪些进了 `opts`（`save-folder`、`sequence-length`、`dry-run`），哪些进了 `overrides`（两条 dotlist），`opts.sequence_length` 又如何影响 `dataset_config`、`data_loader_config` 之外的三处（`max_target_sequence_length`、`rank_microbatch_size=4*4096` 中的显式 4096 是否受它影响？——注意 L67 写死的是 `4 * 4096`，与 `sequence_length` 变量无关，这是一个值得指出的「坑」）。
2. **动态验证一**：实际运行上面的命令（dry-run 不需要 GPU 和数据），在打印出的配置树中核对：`model` 是否仍为 32B（未被覆盖）、`train_module.optim.lr` 是否为 `0.0004`、`dataset.sequence_length` 是否为 `2048`、`train_module.rank_microbatch_size` 是否仍为 `16384`。
3. **动态验证二**：用 4.3.4 的 `run_exp.py` 对自己生成的 `base_config.yaml` 施加同样的两条 dotlist 覆盖，确认两条路径（官方脚本的 `merge(overrides)` 与你的 `from_yaml(overrides=...)`）产生一致的覆盖语义；再用 4.1.4 的 `merge_playground.py` 复现 `6e-3 → 0.006` 的类型解析。

**验收标准**：能向同伴讲清「一条覆盖参数从 shell 到配置字段的五站旅程」：shell → `parse_known_args` 分流 → `_clean_opt` 清洗（YAML 解析、连字符归一）→ `_set_nested` 字典下钻 → `from_dict` 按 `_CLASS_` 重建。运行输出**待本地验证**。

## 6. 本讲小结

- **覆盖引擎**：`Config.merge()` 走「`_clean_opt` 清洗 → `as_dict` 降级 → `_set_nested` 下钻 → `from_dict` 重建」四步；值按 YAML 规则解析（`6e-3`→`0.006`、无 `=` 默认 `true`、`-` 归一为 `_`），返回新对象，多态子类身份靠 `_CLASS_`/`type` 双线索完整保留。
- **统一 CLI**：`get_cli_parser()` 用 `parse_known_args()` 把参数分成「7 个正式选项 + 任意 dotlist 覆盖」；`--save-folder` 是唯一必填项，`--dry-run` 可在零 GPU 零数据环境下预览最终配置。
- **标准入口**：`ExperimentConfig` 是五块积木（model/dataset/data_loader/train_module/trainer）的顶层集装箱；`main(config_builder)` 固定执行「解析 → 构建配置 → 环境准备 → 组件 build → 配置落盘 → 恢复 → fit」。
- **可复现闭环**：命令行覆盖的最终结果经 `ConfigSaverCallback` 写进每个 checkpoint 的 `config.json`，并同步到 WandB/Comet——事后随时还原当时生效的完整配置。
- **两种等价写法**：`config.merge(overrides)` 与 `ExperimentConfig.from_yaml(path, overrides=overrides)` 共用同一引擎，后者只解码一次，更适合「YAML + 命令行微调」的入口脚本。

## 7. 下一步学习建议

本讲之后，配置系统的三板斧（u2-l1 序列化、u2-l2 多态、u2-l3 命令行覆盖）已集齐，你可以沿两条路线继续：

1. **向下深入模型**：进入第三单元，从 [u3-l1 Transformer 总体架构](u3-l1-transformer-architecture.md) 开始精读 `Transformer.forward` 的数据流——你在本讲反复覆盖的 `model` 字段，在那里会被逐层拆开。
2. **向后看完整脚本**：若你想先看官方脚本如何组合全部积木（包括本讲略过的 `TrainerConfig.with_callback` 链式注册），可跳读 [u9-l1 官方训练脚本解析](u9-l1-official-scripts.md)，再回头按顺序补第三单元。

建议顺手阅读的源码：`src/examples/llm/train.py`（自包含版入口，与本讲 `script_utils.main` 对照），以及 `src/test/config_test.py`（merge 行为的最小权威断言，也是你为自定义 Config 写测试的模板）。
