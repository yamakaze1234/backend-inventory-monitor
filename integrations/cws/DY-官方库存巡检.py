"""正式巡检入口：平台登录与浏览器由 CWS 管理，结果交回工作台。"""
from dataclasses import dataclass
from pathlib import Path
import json
import sys
from typing import Annotated, Literal

from scraper.script_api import ScriptBase


@dataclass(frozen=True)
class Row:
    店铺: Annotated[str, '页面已核验店铺名称']
    结果JSON: Annotated[str, '官方导出完整性校验结果或原任务待续取标记']


class Script(ScriptBase):
    platform = 'DY'
    description = '核验指定店铺后导出全量商品表，返回整机 SKU；供工作台定时巡检调用'
    tags = ['库存', '巡检']
    home_url = 'https://fxg.jinritemai.com/ffa/g/list'

    def run(self, 店铺: str, 状态目录: str, 操作: Literal['login', 'collect'] = 'collect') -> list[Row]:
        """每店状态目录由工作台提供，报表等待不重复提交。"""
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'support'))
        from douyin_cws_export import CwsExportWorker
        from douyin_export import ReportPending
        worker = CwsExportWorker(self, 状态目录, 店铺)
        try:
            result = worker.call(操作)
        except ReportPending:
            result = {'pending': True}
        return [Row(店铺, json.dumps(result, ensure_ascii=False))]
