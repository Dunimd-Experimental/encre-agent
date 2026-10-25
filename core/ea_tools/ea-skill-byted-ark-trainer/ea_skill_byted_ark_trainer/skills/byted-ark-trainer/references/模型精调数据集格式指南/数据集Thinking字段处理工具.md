<span id="9656cd75"></span>
# 附3：数据集Thinking字段处理工具
您可以通过脚本将不含`thinking`的数据集转换为含`thinking`的数据集：

* 对于数据集中不含`reasoning_content`的样本，将`thinking`赋值为`disabled`。
* 含`reasoning_content`的样本，则赋值为`enabled`。

您可下载 [此脚本](https://ark-project.tos-cn-beijing.volces.com/jsonl/dataset_converter.zip) 对数据集进行转换，使您的数据集符合精调模型数据格式规范。