"""xaj model"""
import math
from typing import Union

import numpy as np
from scipy import signal

from src.gr4j.gr4j import s_curves2


def calculate_evap(lm, c,
                   wu0, wl0,
                   prcp, pet):
    """时段蒸发计算，三层蒸发模型 from <<SHUIWEN YUBAO>> the fifth version. Page 22-23

    Parameters
    ----------
    lm, c: 三层蒸发模型计算所需参数
    wu0, wl0: 三层蒸发模型计算所需初始条件
    prcp, pet: 流域面平均降雨量, potential evapotranspiration

    Returns
    -------
    out : float
        eu,el,ed:流域时段三层蒸散发
    """
    eu = np.where(wu0 + prcp >= pet, pet, wu0 + prcp)
    ed = np.where((wl0 < c * lm) & (wl0 < c * (pet - eu)), c * (pet - eu) - wl0, 0)
    el = np.where(wu0 + prcp >= pet,
                  0,
                  np.where(wl0 >= c * lm, (pet - eu) * wl0 / lm,
                           np.where(wl0 >= c * (pet - eu), c * (pet - eu), wl0)))
    return eu, el, ed


def calculate_prcp_runoff(b, im, wm,
                          w0,
                          pe):
    """Calculates the amount of runoff generated from rainfall after entering the underlying surface
    Parameters
    ----------
    b, im, wm:
        计算所需参数
    w0:
        计算所需初始条件
    pe:
        net precipitation

    Returns
    -------
    out :
       r, r_im: runoff
    """
    wmm = wm * (1 + b) / (1 - im)
    a = wmm * (1 - (1 - w0 / wm) ** (1 / (1 + b)))
    # when pe==0, this must get 0, so no need to see the case "if pe<=0"
    r_cal = np.where(pe + a < wmm,
                     pe - (wm - w0) + wm * (1 - (a + pe) / wmm) ** (1 + b),
                     pe - (wm - w0))
    r = np.maximum(r_cal, 0)
    r_im_cal = pe * im
    r_im = np.maximum(r_im_cal, 0)
    return r, r_im


def calculate_w_storage(um, lm, dm,
                        wu0, wl0, wd0, eu, el, ed,
                        p, r):
    """update the w values of the three layers
       according to the runoff-generation equation 2.60, dW = dPE - dR,
       which means that for one period: the change of w = pe - r
    Parameters
    ----------
    um, lm, dm: 计算所需参数
    wu0, wl0, wd0, eu, el, ed: 计算所需state variables
    p, r: 流域面平均降雨量, and runoff

    Returns
    -------
    out : float
        eu,el,ed:流域时段三层蒸散发
        wu,wl,wd:流域时段三层含水量
    """
    e = eu + el + ed
    # net precipitation
    pe = np.maximum(p - e, 0)
    # 当pe>0时，说明流域蓄水量增加，首先补充上层土壤水，然后补充下层，最后补充深层
    # pe<=0: no additional water, just remove evapotranspiration
    wu = np.where(pe > 0, np.where(wu0 + pe - r < um, wu0 + pe - r, um), wu0 - eu)
    # calculate wd before wl because it is easier to cal using where statement
    wd = np.where(pe > 0, np.where(wu0 + wl0 + pe - r > um + lm, wu0 + wl0 + wd0 + pe - r - um - lm, wd0), wd0 - ed)
    # water balance (equation 2.2 in Page 13, also shown in Page 23)
    wl = np.where(pe > 0, wu0 + wl0 + wd0 + pe - r - wu - wd, wl0 - el)
    # 可能有计算误差使得数据略超出合理范围，应该规避掉，如果明显超出范围，则可能计算有误，应仔细检查计算过程
    wu_ = np.clip(wu, 0, um)
    wl_ = np.clip(wl, 0, lm)
    wd_ = np.clip(wd, 0, dm)
    return wu_, wl_, wd_


def generation(p_and_e,
               um, lm, dm, c, b, im,
               wu0=None, wl0=None, wd0=None):
    """
    Parameters
    ----------
    p_and_e: precipitation and potential evapotranspiration
    um, lm, dm, c, b, im: parameters
    wu0, wl0, wd0: state variables

    Returns
    -------
    S: Storage reservoir level at the end of the timestep
    """
    # 为了防止后续计算出现不符合物理意义的情况，这里要对p和e的取值进行范围限制
    prcp = np.maximum(p_and_e[:, 0:1], 0)
    pet = np.maximum(p_and_e[:, 1:2], 0)
    # wm
    wm = um + lm + dm
    if wu0 is None:
        # a empirical value
        wu0 = 0.6 * um
    if wl0 is None:
        # a empirical value
        wl0 = 0.6 * lm
    if wd0 is None:
        # a empirical value
        wd0 = 0.6 * dm
    w0_ = wu0 + wl0 + wd0
    # 注意计算a时，开方运算，偶数方次时，根号下不能为负数，所以需要限制w0的取值，这也是物理意义上的要求
    if w0_ > wm:
        w0 = wm
    else:
        w0 = w0_

    # Calculate the amount of evaporation from storage
    eu, el, ed = calculate_evap(lm, c,
                                wu0, wl0,
                                prcp, pet)
    e = eu + el + ed

    # Calculate the runoff generated by net precipitation
    prcp_difference = prcp - e
    pe = np.maximum(prcp_difference, 0)
    r, rim = calculate_prcp_runoff(b, im, wm,
                                   w0,
                                   pe)
    # Update wu, wl, wd
    wu, wl, wd = calculate_w_storage(um, lm, dm,
                                     wu0, wl0, wd0, eu, el, ed,
                                     pe, r)

    # The order of the returned values is important because it must correspond
    # up with the order of the kwarg list argument 'outputs_info' to lax.scan.
    return (r, rim, e, pe), (wu, wl, wd)


def sources(pe, r,
            sm, ex, ki, kg,
            s0=None, fr0=None):
    """分水源计算  from <<SHUIWEN YUBAO>> the fifth version. Page 40-41 and 150-151
        the procedures in <<GONGCHENG SHUIWENXUE>> the third version are different.
        Here we used the former. We'll add the latter in the future.
    Parameters
    ------------
    pe: net precipitation
    r: 产流
    sm, ex, ki, kg: required parameters
    s0, fr0: 计算所需初始条件 initial_conditions
    Return
    ------------
    rs,ri,rg:
        除不透水面积以外的面积上划分水源得到的地表径流，壤中流和地下径流，最后将水深值从不透水面积折算到流域面积

    """
    # 流域最大点自由水蓄水容量深
    ms = sm * (1 + ex)
    if s0 is None:
        s0 = 0.60 * sm
    if fr0 is None:
        fr0 = 0.02
    # FR of this period  equation 5.24. However, we should notice that when pe=0,
    # we think no change occurred in S, so fr = fr0 and s = s0
    fr = np.where(pe < 1e-5, fr0, r / pe)

    # we don't know how the equation 5.32 was derived, so we don't divide the Runoff here.
    # equation 2.84
    au = ms * (1 - (1 - (fr0 * s0 / fr) / sm) ** (1 / (1 + ex)))
    # when pe==0, this equation must lead to "rs=0", so no need to consider the case "if pe<=0"
    rs = np.where(pe + au < ms,
                  # equation 2.85
                  fr * (pe + (fr0 * s0 / fr) - sm + sm * (1 - (pe + au) / ms) ** (ex + 1)),
                  # equation 2.86
                  fr * (pe + (fr0 * s0 / fr) - sm))
    # equation 2.87
    s = (fr0 * s0 / fr) + (r - rs) / fr
    # equation 2.88
    ri = ki * s * fr
    rg = kg * s * fr
    s1 = s * (1 - ki - kg)

    # maybe there are some very small negative values
    rs = np.maximum(rs, 0)
    ri = np.maximum(ri, 0)
    rg = np.maximum(rg, 0)
    return (rs, ri, rg), (fr, s1)


def sources5mm(pe, runoff,
               sm, ex, ki, kg,
               s0=None, fr0=None,
               time_interval_hours=24,
               book="ShuiWenYuBao"):
    """分水源计算 according to books -- <<ShuiWenYuBao>> 5th edition and <<GongChengShuiWenXue>> 3rd edition
    Although I don't think the methods provided in books are good (very tedious and the explanation is not clear),
    they are still provided here.

    Parameters
    ------------
    pe: net precipitation
    runoff: 产流
    sm, ex, ki, kg: 分水源计算所需参数
    s0, fr0: 计算所需初始条件
    time_interval_hours: 由于Ki、Kg、Ci、Cg都是以24小时为时段长定义的，需根据时段长转换
    book: the methods in <<ShuiWenYuBao>> 5th edition and <<GongChengShuiWenXue>> 3rd edition are different,
          hence, both are provided, and the default is the former.

    Return
    ------------
    rs_s,rss_s,rg_s: 除不透水面积以外的面积上划分水源得到的地表径流，壤中流和地下径流，最后将水深值从不透水面积折算到流域面积

    """
    # 由于Ki、Kg都是以24小时为时段长定义的，需根据时段长转换
    hours_per_day = 24
    # 非整除情况，时段+1
    residue_temp = hours_per_day % time_interval_hours
    if residue_temp != 0:
        residue_temp = 1
    period_num_1d = int(hours_per_day / time_interval_hours) + residue_temp
    # 当kss+kg>1时，根式为偶数运算时，kss_period会成为复数，这里会报错；另外注意分母可能为0，kss不可取0
    # 对kss+kg的取值进行限制，也是符合物理意义的，地下水出流不能超过自身的蓄水。
    kss_period = (1 - (1 - (ki + kg)) ** (1 / period_num_1d)) / (1 + kg / ki)
    kg_period = kss_period * kg / ki

    # 流域最大点自由水蓄水容量深
    smm = sm * (1 + ex)
    if s0 is None:
        s0 = 0.60 * sm
    if fr0 is None:
        fr0 = 0.02
    fr = np.where(pe > 1e-5, runoff / pe, fr0)
    fr = np.clip(fr, 0.001, 1)

    # 净雨分5mm一段进行计算，因为计算时在FS/FR ~ SMF'关系图上开展，即计算在产流面积上开展，所以用PE做净雨.分段为了差分计算更精确。
    if runoff < 5:
        n = 1
    else:
        residue_temp = runoff % 5
        if residue_temp != 0:
            residue_temp = 1
        n = int(runoff / 5) + residue_temp
    # 整除了就是5mm，不整除就少一些，差分每段小了也挺好
    rn = runoff / n
    pen = pe / n
    kss_d = (1 - (1 - (kss_period + kg_period)) ** (1 / n)) / (1 + kg_period / kss_period)
    kg_d = kss_d * kg_period / kss_period

    rs = rss = rg = 0

    s_ds = []
    fr_ds = []
    s_ds.append(s0)
    fr_ds.append(fr0)

    for j in range(n):
        # 因为产流面积随着自由水蓄水容量的变化而变化，每5mm净雨对应的产流面积肯定是不同的，因此fr是变化的
        fr0_d = fr_ds[j]
        s0_d = s_ds[j]
        fr_d = 1 - (1 - fr) ** (1 / n)
        s_d = fr0_d * s0_d / fr_d

        if book == "ShuiWenYuBao":
            ms = smm
            if s_d > sm:
                s_d = sm
            au = ms * (1 - (1 - s_d / sm) ** (1 / (1 + ex)))
            if pen + au >= ms:
                rs_j = (pen + s_d - sm) * fr_d
            else:
                rs_j = (pen - sm + s_d + sm * (1 - (pen + au) / ms) ** (ex + 1)) * fr_d
            s_d = s_d + (rn - rs_j) / fr_d
            rss_j = s_d * kss_d * fr_d
            rg_j = s_d * kg_d * fr_d
            s_d = s_d * (1 - rss_j + rg_j)

        else:
            smmf = smm * (1 - (1 - fr_d) ** (1 / ex))
            smf = smmf / (1 + ex)
            # 如果出现s_d>smf的情况，说明s_d = fr0_d * s0_d / fr_d导致的计算误差不合理，需要进行修正。
            if s_d > smf:
                s_d = smf
            au = smmf * (1 - (1 - s_d / smf) ** (1 / (1 + ex)))
            if pen + au >= smmf:
                rs_j = (pen + s_d - smf) * fr_d
                rss_j = smf * kss_d * fr_d
                rg_j = smf * kg_d * fr_d
                s_d = smf - (rss_j + rg_j) / fr_d
            else:
                rs_j = (pen - smf + s_d + smf * (1 - (pen + au) / smmf) ** (ex + 1)) * fr_d
                rss_j = (pen - rs_j / fr_d + s_d) * kss_d * fr_d
                rg_j = (pen - rs_j / fr_d + s_d) * kg_d * fr_d
                s_d = s_d + pen - (rs_j + rss_j + rg_j) / fr_d

        rs = rs + rs_j
        rss = rss + rss_j
        rg = rg + rg_j
        # 赋值s_d和fr_d到数组中，以给下一段做初值
        s_ds.append(s_d)
        fr_ds.append(fr_d)

    return (rs, rss, rg), (fr_ds[-1], s_ds[-1])


def xaj(p_and_e, params, states=None, uh: Union[list, int, float] = None, source_type="sources",
        source_book="ShuiWenYuBao"):
    """
    Parameters
    ----------
    p_and_e: prcp and pet
    params: the parameters
    states: the initial states
    uh: unit hydrograph, when it is None, we use linear reservoir model to represent the route module of surface runoff
    source_type: when using "sources5mm", we will divide the runoff to some <5mm pieces according to the books
    source_book: books include <<ShuiWenYuBao>> 5th edition and <<GongChengShuiWenXue>> 3rd edition,
                however, the methods in these two books are different,
                hence, both are provided, and the default is the former.

    Returns
    -------
    streamflow
    """
    # params
    b = params['B']
    im = params['IM']
    um = params['UM']
    lm = params['LM']
    dm = params['DM']
    c = params['C']
    sm = params['SM']
    ex = params['EX']
    ki = params['KI']
    kg = params['KG']
    ci = params['CI']
    cg = params['CG']
    surface_route_method = "linear_reservoir"
    if uh is None:
        # use linear reservoir to represent the routing module of surface runoff
        cs = params['CS']
    else:
        surface_route_method = "unit_hydrograph"
        if type(uh) is not list:
            # we use s_curve method in GR4J to generate a unit hydrograph temporally
            nUH = int(math.ceil(2.0 * uh))
            uh_ordinates = [0] * nUH
            for t in range(1, nUH + 1):
                uh_ordinates[t - 1] = s_curves2(t, uh) - s_curves2(t - 1, uh)
        else:
            uh_ordinates = uh
        UH = np.array(uh_ordinates).reshape(1, 1, -1)

    # state_variables
    if states is None:
        wu0 = None
        wl0 = None
        wd0 = None
        s0 = None
        fr0 = None
        qs0 = None
        qi0 = None
        qg0 = None
    else:
        wu0 = states["WU0"]
        wl0 = states["WL0"]
        wd0 = states["WD0"]
        s0 = states["S0"]
        fr0 = states["FR0"]
        qs0 = states["QS0"]
        qi0 = states["QI0"]
        qg0 = states["QG0"]

    runoff_ims_ = []
    rss_ = []
    ris_ = []
    rgs_ = []
    for i in range(p_and_e.shape[1]):
        if i == 0:
            (r, rim, e, pe), w = generation(p_and_e[:, i, :],
                                            um, lm, dm, c, b, im,
                                            wu0, wl0, wd0)
            if source_type == "sources":
                (rs, ri, rg), S = sources(pe, r,
                                          sm, ex, ki, kg,
                                          s0, fr0)
            elif source_type == "sources5mm":
                (rs, ri, rg), S = sources5mm(pe, r,
                                             sm, ex, ki, kg,
                                             s0, fr0,
                                             book=source_book)
            else:
                raise NotImplementedError("No such divide-sources method")
        else:
            (r, rim, e, pe), w = generation(p_and_e[:, i, :],
                                            um, lm, dm, c, b, im,
                                            *w)
            if source_type == "sources":
                (rs, ri, rg), S = sources(pe, r,
                                          sm, ex, ki, kg,
                                          *S)
            elif source_type == "sources5mm":
                (rs, ri, rg), S = sources5mm(pe, r,
                                             sm, ex, ki, kg,
                                             *S,
                                             book=source_book)
            else:
                raise NotImplementedError("No such divide-sources method")
        runoff_ims_.append(rim)
        rss_.append(rs)
        ris_.append(ri)
        rgs_.append(rg)
    # batch, seq, feature
    runoff_im = np.stack(runoff_ims_, axis=1)
    rss = np.stack(rss_, axis=1)

    if surface_route_method == "unit_hydrograph":
        rss_route = np.swapaxes(runoff_im + rss, 1, 2)
        qs_ = signal.convolve(rss_route, UH)
    else:
        rss_route = runoff_im + rss

    Qs = []
    for i in range(p_and_e.shape[1]):
        if i == 0:
            if surface_route_method == "linear_reservoir":
                if qs0 is None:
                    qs0 = 0.01
                qs = rss_route[:, i, :] * (1 - cs) + qs0 * cs
            else:
                qs = qs_[:, :, i]
            if qi0 is None:
                qi0 = 0.01
            if qg0 is None:
                qg0 = 0.01
            qi = ris_[i] * (1 - ci) + qi0 * ci
            qg = rgs_[i] * (1 - cg) + qg0 * cg
        else:
            if surface_route_method == "linear_reservoir":
                qs = rss_route[:, i, :] * (1 - cs) + qs * cs
            else:
                qs = qs_[:, :, i]
            qi = ris_[i] * (1 - ci) + qi * ci
            qg = rgs_[i] * (1 - cg) + qg * cg
        q = qs + qi + qg
        Qs.append(q)
    # batch, seq, feature
    Q = np.stack(Qs, axis=1)
    return Q