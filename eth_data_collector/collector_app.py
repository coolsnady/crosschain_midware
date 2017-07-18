#!/usr/bin/env python
# encoding=utf8

__author__ = 'hasee'

######################################################################
#  数据处理逻辑：
#  1. 先从数据库中获取出上次采集已成功提交的区块号
#  2. 采集前清理掉超出此区块号的tbl_block, tbl_transaction, tbl_transaction_ex, tbl_contract_info表中相关记录
#  3. 考虑到对同一个合约的操作，可能会有并发问题导致的合约操作的先后顺序颠倒的问题，
#       对于tbl_contract_info表，采用replace into ON DUPLICATE的方式
#  4. 对于tbl_contract_abi, tbl_contract_storage, tbl_contract_event表，在遇到注册相关合约相关的交易的处理上，
#       先清理，再插入
######################################################################





import sys
import json
import logging, traceback
from collector_conf import SYNC_BLOCK_PER_ROUND
from collector_conf import REFRESH_STAT_POST_URL, REFRESH_STAT_POST_DATA
# from utility import to_utf8
# from base import TRX_TYPE_REGISTER_CONTRACT, TRX_TYPE_UPGRADE_CONTRACT, TRX_TYPE_DESTROY_CONTRACT
from base import GlobalVariable
from eth_utils import eth_request
# from httprequest import do_post
# import rpc_biz
import time
from block import BlockInfo
from datetime import datetime
import math


def do_collect_app(db):
    while True:
        try:
            # 程序启动，设置为同步状态
            config = db.b_config
            config.update({"key": "syncstate"}, {"key": "syncstate", "value": True})

            # 清理上一轮的垃圾数据，包括块数据、交易数据以及合约数据
            GlobalVariable.last_sync_block_num = clear_last_garbage_data(db)
            # 获取当前链上最新块号
            while True:
                GlobalVariable.register_account_dic = {}
                GlobalVariable.upgrade_contract_dic = {}
                latest_block_num = get_latest_block_num(db)
                init_account_info(db)
                print "latest_block_num", latest_block_num
                print "GlobalVariable.last_sync_block_num", GlobalVariable.last_sync_block_num
                if GlobalVariable.last_sync_block_num >= latest_block_num:
                    GlobalVariable.sync_start_per_round = latest_block_num
                    GlobalVariable.sync_end_per_round = latest_block_num
                else:
                    # print 2
                    GlobalVariable.sync_start_per_round = GlobalVariable.last_sync_block_num + 1
                    GlobalVariable.sync_end_per_round = ((
                                                             GlobalVariable.last_sync_block_num + SYNC_BLOCK_PER_ROUND) >= latest_block_num) \
                                                        and latest_block_num or (
                                                            GlobalVariable.last_sync_block_num + SYNC_BLOCK_PER_ROUND)
                print GlobalVariable.sync_start_per_round
                print latest_block_num
                sync_rate = float(GlobalVariable.sync_start_per_round) / latest_block_num
                sync_process = '#' * int(40 * sync_rate) + ' ' * (40 - int(40 * sync_rate))
                sys.stdout.write(
                    "\rsync block [%s][%d/%d], %.3f%%\n" % (sync_process, GlobalVariable.sync_start_per_round,
                                                            latest_block_num, sync_rate * 100))
                while GlobalVariable.sync_start_per_round < GlobalVariable.sync_end_per_round:
                    collect_data_cb(db)
                GlobalVariable.last_sync_block_num = GlobalVariable.sync_end_per_round
                config.update({"key": "syncblocknum"},
                              {"$set": {"key": "syncblocknum", "value": str(GlobalVariable.last_sync_block_num)}})
                if GlobalVariable.sync_start_per_round == latest_block_num:
                    break

            print 'ok'
            time.sleep(10)


        except Exception, ex:
            logging.info(traceback.format_exc())
            print ex
            # 异常情况，60秒后重试eth_utils.py
            time.sleep(60)
            do_collect_app(db)


def init_account_info(db):
    GlobalVariable.db_account_list = []
    records = db.b_chain_account.find({"chainId": "eth"})
    for one_account in records:
        GlobalVariable.db_account_list.append(one_account["address"])

    cash_sweep_data = db.b_config.find_one({"key": "cash_sweep_address"})
    if cash_sweep_data is not None:

        for data in cash_sweep_data["value"]:
            if data["chainId"] == "eth":
                GlobalVariable.cash_sweep_account.append(data["address"])
                break

    withdraw_data = db.b_config.find_one({"key": "withdrawaddress"})
    if withdraw_data is not None:

        for data in withdraw_data["value"]:
            if data["chainId"] == "eth":
                GlobalVariable.withdraw_account.append(data["address"])
                break

    ret = eth_request("personal_listAccounts", [])
    json_data = json.loads(ret)
    if json_data.get("result") is None:
        raise Exception("get_all_account_list")
    GlobalVariable.account_list = json_data["result"]


def get_latest_block_num(db):
    ret = eth_request("eth_blockNumber", [])
    json_data = json.loads(ret)
    safe_block = db.b_config.find_one({"key": "safeblock"})["value"]
    return int(json_data["result"], 16) - int(safe_block)


def clear_last_garbage_data(db_pool):
    config = db_pool.b_config
    ret = config.find_one({"key": "syncblocknum"})
    if ret is None:
        config.insert({"key": "syncblocknum", "value": "0"})
        last_sync_block_num = int(0)
    else:
        last_sync_block_num = int(ret["value"])
    try:
        db_pool.b_raw_transaction.remove({"blockNum": {"$gte": last_sync_block_num}, "chainId": "eth"})
        db_pool.b_block.remove({"blockNumber": {"$gte": last_sync_block_num}, "chainId": "eth"})

        db_pool.b_raw_transaction_input.remove({"blockNum": {"$gte": last_sync_block_num}, "chainId": "eth"})
        db_pool.b_raw_transaction_output.remove({"blockNum": {"$gte": last_sync_block_num}, "chainId": "eth"})
        db_pool.b_deposit_transaction.remove({"blockNum": {"$gte": last_sync_block_num}, "chainId": "eth"})
        db_pool.b_withdraw_transaction.remove({"blockNum": {"$gte": last_sync_block_num}, "chainId": "eth"})
    except Exception, ex:
        logging.info(traceback.format_exc())
        print ex
    return int(last_sync_block_num)


# 采集块数据
def collect_block(db_pool, block_num_fetch):
    ret = eth_request("eth_getBlockByNumber", [hex(block_num_fetch), False])

    # ret = eth_request("eth_getBlock", [str(block_num_fetch)])
    print "", block_num_fetch
    if json.loads(ret).get("result") is None:
        # 正式环境删除
        while True:
            block_num_fetch = GlobalVariable.sync_start_per_round

            GlobalVariable.sync_start_per_round += 1
            if GlobalVariable.sync_start_per_round > GlobalVariable.sync_end_per_round:
                GlobalVariable.sync_end_per_round = GlobalVariable.sync_start_per_round
            print hex(block_num_fetch)
            ret = eth_request("eth_getBlockByNumber", [hex(block_num_fetch), False])
            print "GlobalVariable.sync_start_per_round", GlobalVariable.sync_start_per_round, ret
            if json.loads(ret).get("result") is not None:
                break

                # raise Exception("blockchain_get_block error blockNum:%d, returnStr: %s"%(block_num_fetch,ret))
    json_data = json.loads(ret)
    # print json_data["result"]
    json_data = json_data["result"]
    # if len(json_data["transactions"]) > 0:
    #    print "has transactions:", block_num_fetch

    block_info = BlockInfo()
    block_info.from_block_resp(json_data)

    block = db_pool.b_block

    mongo_data = block.find_one({"blockHash": block_info.block_id})
    # print {"blockHash":block_info.block_id}
    if mongo_data == None:
        block.insert(block_info.get_json_data())
    else:
        block.update({"blockHash": block_info.block_id}, {"$set": block_info.get_json_data()})

    # print 1

    return block_info


def is_care_trx(receipt_data):
    temp_list = []
    temp_list.extend(GlobalVariable.account_list)
    temp_list.extend(GlobalVariable.db_account_list)
    temp_list.extend(GlobalVariable.cash_sweep_account)
    temp_list.extend(GlobalVariable.withdraw_account)
    if receipt_data["from"] in temp_list:
        return True
    if receipt_data["to"] in temp_list:
        return True
    return False


def get_transaction_data(trx_id):
    # print "\ntrx_id:",trx_id
    ret = eth_request("eth_getTransactionByHash", [str(trx_id)])
    # print ret
    json_data = json.loads(ret)
    if json_data.get("result") is None:
        raise Exception("blockchain_get_transaction error trx_id:%s, returnStr: %s" % (trx_id, ret))
    resp_data = json_data.get("result")
    ret = eth_request("eth_getTransactionReceipt", [str(trx_id)])
    json_data = json.loads(ret)
    if json_data.get("result") is None:
        raise Exception("blockchain_get_transaction_receipt error  trx_id:%s, returnStr: %s" % (trx_id, ret))
    receipt_data = json_data.get("result")
    return resp_data, receipt_data


def is_contract_trx(receipt_data):
    if receipt_data["contractAddress"] is not None:
        return True
    ret = eth_request("eth_getCode", [receipt_data["to"], "latest"])
    json_data = json.loads(ret)
    if json_data.get("result") is None:
        raise Exception("is contract trx error")

    if json_data["result"] == "0x":
        return False
    else:
        return True


def collect_pretty_transaction(db_pool, base_trx_data, receipt_trx_data, block_time):
    raw_transaction_db = db_pool.b_raw_transaction
    trx_data = {}
    trx_data["chainId"] = "eth"
    trx_data["trxid"] = base_trx_data["hash"]
    trx_data["blockid"] = base_trx_data["blockHash"]
    trx_data["blockNum"] = int(base_trx_data["blockNumber"], 16)
    trx_data["fromAddresses"] = [receipt_trx_data["from"]]
    trx_data["fromAmounts"] = [str(float(int(base_trx_data["value"], 16)) / pow(10, 18))]
    trx_data["fromAssets"] = ["ETH"]
    trx_data["toAddresses"] = [receipt_trx_data["to"]]

    trx_data["toAmounts"] = [str(float(int(base_trx_data["value"], 16)) / pow(10, 18))]
    trx_data["toAssets"] = ["ETH"]
    trx_data["trxFee"] = str(
        float((int(receipt_trx_data["gasUsed"], 16)) * (int(base_trx_data["gasPrice"], 16))) / pow(10, 18))
    trx_data["FeeAsset"] = "ETH"
    trx_data["isSpecialTransaction"] = is_contract_trx(receipt_trx_data)
    trx_data["transactionJsonInfo"] = base_trx_data
    trx_data["memo"] = ""
    trx_data["trxTime"] = block_time
    trx_data["createtime"] = datetime.now()
    trx_data["isDispatched"] = 0
    trx_data["isHandled"] = 0
    mongo_data = raw_transaction_db.find_one({"trxid": base_trx_data["hash"]})
    if mongo_data == None:
        raw_transaction_db.insert(trx_data)
    else:
        raw_transaction_db.update({"trxid": base_trx_data["hash"]}, {"$set": trx_data})

    raw_transaction_input_db = db_pool.b_raw_transaction_input

    trx_input_data = {}
    trx_input_data["chainId"] = "eth"
    trx_input_data["rawTransactionId"] = trx_data["trxid"]
    trx_input_data["blockNum"] = trx_data["blockNum"]
    trx_input_data["address"] = receipt_trx_data["from"]
    trx_input_data["assetName"] = "eth"
    trx_input_data["amount"] = str(float(int(base_trx_data["value"], 16)) / pow(10, 18))
    mongo_data = raw_transaction_input_db.find_one(
        {"trxid": base_trx_data["hash"], "address": receipt_trx_data["from"]})
    if mongo_data == None:
        raw_transaction_input_db.insert(trx_input_data)
    else:
        raw_transaction_input_db.update({"trxid": base_trx_data["hash"], "address": receipt_trx_data["from"]},
                                        {"$set": trx_data})

    raw_transaction_output_db = db_pool.b_raw_transaction_output
    trx_output_data = {}
    trx_output_data["chainId"] = "eth"
    trx_output_data["rawTransactionId"] = trx_data["trxid"]
    trx_output_data["blockNum"] = trx_data["blockNum"]
    trx_output_data["address"] = receipt_trx_data["to"]
    trx_output_data["assetName"] = "eth"
    trx_output_data["amount"] = str(float(int(base_trx_data["value"], 16)) / pow(10, 18))
    mongo_data = raw_transaction_output_db.find_one({"trxid": base_trx_data["hash"], "address": receipt_trx_data["to"]})
    if mongo_data == None:
        raw_transaction_output_db.insert(trx_output_data)
    else:
        raw_transaction_output_db.update({"trxid": base_trx_data["hash"], "address": receipt_trx_data["from"]},
                                         {"$set": trx_data})

    if receipt_trx_data["from"] in GlobalVariable.withdraw_account:
        # 提现交易搬运
        b_withdraw_transaction = db_pool.b_withdraw_transaction
        withdraw_data_trx = b_withdraw_transaction.find_one({"chainId": "eth", "TransactionId": trx_data["trxid"]})
        withdraw_trx = {"chainId": "eth", "TransactionId": trx_data["trxid"], "toAddress": receipt_trx_data["to"],
                        "fromAccount": receipt_trx_data["from"],
                        "assetName": "eth", "status": 2, "amount": float(int(base_trx_data["value"], 16)) / pow(10, 18),
                        "blockNum": trx_data["blockNum"], "trxTime": block_time}
        if withdraw_data_trx is None:
            b_withdraw_transaction.insert(withdraw_trx)
        else:
            b_withdraw_transaction.update({"TransactionId": trx_data["trxid"]}, {"$set": withdraw_trx})

    if receipt_trx_data["to"] in GlobalVariable.db_account_list:
        # 入账交易搬运
        b_deposit_transaction = db_pool.b_deposit_transaction
        deposit_data_trx = b_deposit_transaction.find_one({"chainId": "eth", "TransactionId": trx_data["trxid"]})
        deposit_trx = {"chainId": "eth", "TransactionId": trx_data["trxid"], "toAddress": receipt_trx_data["to"],
                       "fromAddress": receipt_trx_data["from"],
                       "assetName": "eth", "amount": float(int(base_trx_data["value"], 16)) / pow(10, 18),
                       "blockNum": trx_data["blockNum"], "trxTime": block_time}
        if deposit_data_trx is None:
            b_deposit_transaction.insert(deposit_trx)
        else:
            b_deposit_transaction.update({"TransactionId": trx_data["trxid"]}, {"$set": deposit_trx})
    if receipt_trx_data["to"] in GlobalVariable.cash_sweep_account:
        # 归账交易搬运
        print "归账交易搬运"
        b_cash_sweep_plan_detail = db_pool.b_cash_sweep_plan_detail
        cash_sweep_data_trx = b_cash_sweep_plan_detail.find_one({"chainId": "eth", "trxId": trx_data["trxid"]})
        cash_sweep_trx = {"chainId": "eth", "trxId": trx_data["trxid"], "sweepAddress": receipt_trx_data["to"],
                          "fromAddress": receipt_trx_data["from"],
                          "successCoinAmount": float(int(base_trx_data["value"], 16)) / pow(10, 18), "status": 1,
                          "blockNum": trx_data["blockNum"], "trxTime": block_time}
        if cash_sweep_data_trx is None:
            b_cash_sweep_plan_detail.insert(cash_sweep_trx)
        else:
            b_cash_sweep_plan_detail.update({"trxId": trx_data["trxid"]}, {"$set": cash_sweep_trx})
            record = b_cash_sweep_plan_detail.find_one(
                {"cash_sweep_id": cash_sweep_data_trx["cash_sweep_id"], "status": 0})
            if record is None:
                db_pool.b_cash_sweep.update({"_id": cash_sweep_data_trx["cash_sweep_id"]}, {"$set": {"status": 2}})

    return trx_data


def update_block_trx_amount(db_pool, block_info):
    block = db_pool.b_block
    block.update({"blockHash": block_info.block_id},
                 {"$set": {"trxamount:": str(block_info.trx_amount), "trxfee": str(block_info.trx_fee)}})


# 采集数据
def collect_data_cb(db_pool):
    try:

        while GlobalVariable.sync_start_per_round <= GlobalVariable.sync_end_per_round:
            block_num_fetch = GlobalVariable.sync_start_per_round
            GlobalVariable.sync_start_per_round += 1

            # 采集块
            block_info = collect_block(db_pool, block_num_fetch)

            for trx_id in block_info.transactions:
                trx_id = trx_id
                # 采集交易
                base_trx_data, receipt_trx_data = get_transaction_data(trx_id)
                if not is_care_trx(receipt_trx_data):
                    continue

                if not is_contract_trx(receipt_trx_data):
                    # 非合约交易
                    pretty_trx_info = collect_pretty_transaction(db_pool, base_trx_data, receipt_trx_data,
                                                                 block_info.block_time)
                    # 统计块中交易总金额和总手续费
                    for amount in pretty_trx_info["toAmounts"]:
                        block_info.trx_amount = block_info.trx_amount + float(amount)

                    block_info.trx_fee = block_info.trx_fee + float(pretty_trx_info["trxFee"])
                else:
                    # print "has contract transaction: ",block_info.block_num
                    pass
                    '''
                    # 合约交易
                    pretty_contract_trx_info = collect_pretty_contract_transaction(rpc, db_pool,
                                                                                                 trx_id,
                                                                                                 block_info.block_id)
                    # 如果是注册合约、升级合约、销毁合约，需要更新合约信息表
                    if pretty_contract_trx_info.trx_type in (TRX_TYPE_REGISTER_CONTRACT,
                                                             TRX_TYPE_UPGRADE_CONTRACT, TRX_TYPE_DESTROY_CONTRACT):
                        yield rpc_biz.collect_contract_info(rpc, db_pool, pretty_contract_trx_info.to_addr,
                                                            pretty_contract_trx_info.trx_type, pretty_contract_trx_info.trx_id,
                                                            pretty_contract_trx_info.trx_time)

                    # 如果是升级合约、销毁合约、合约充值、合约调用，需要更新合约balance
                    if pretty_contract_trx_info.trx_type != TRX_TYPE_REGISTER_CONTRACT:
                        yield rpc_biz.collect_contract_balance(rpc, db_pool, pretty_contract_trx_info.to_addr)

                    # 统计块中交易总金额和总手续费
                    block_info.trx_amount = block_info.trx_amount + pretty_contract_trx_info.amount
                    block_info.trx_fee = block_info.trx_fee + pretty_contract_trx_info.fee
                    for extra_trx in pretty_contract_trx_info.extra_trx_list:
                        block_info.trx_amount = block_info.trx_amount + extra_trx.amount
                        block_info.trx_fee = block_info.trx_fee + extra_trx.fee
                    '''

            if block_info.trx_amount > 0 or block_info.trx_fee > 0:
                update_block_trx_amount(db_pool, block_info)

                # 连接使用完毕，需要释放连接

    except Exception, ex:
        raise ex


if __name__ == "__main__":
    init_account_info()
