# localization.py: HEALPix and associated localization classes
#
#     Authors: William Cleveland (USRA),
#              Adam Goldstein (USRA) and
#              Daniel Kocevski (NASA)
#
#     Portions of the code are Copyright 2020 William Cleveland and
#     Adam Goldstein, Universities Space Research Association
#     All rights reserved.
#
#     Written for the Fermi Gamma-ray Burst Monitor (Fermi-GBM)
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
import os, re
from copy import deepcopy
import astropy.io.fits as fits
import numpy as np
from scipy.stats import chi2, norm
from warnings import warn
import warnings

import healpy as hp
from collections import OrderedDict
from matplotlib.pyplot import contour as Contour
from matplotlib.patches import Polygon
from .lal_post_subs import make_circle_poly
from .coords import get_sun_loc, geocenter_in_radec, spacecraft_to_radec
from .coords import latitude_from_geocentric_coords_complex, haversine
from .coords import latitude_from_geocentric_coords_simple
from .detectors import Detector
from .data import DataFile
from .headers import healpix_primary, healpix_image


class HealPix(DataFile):
    """Base class for HEALPix localization files.
    
    Attributes:
        centroid (float, float): The RA and Dec of the highest probability pixel
        datatype (str): The datatype of the file
        detector (str): The GBM detector the file is associated with
        directory (str): The directory the file is located in
        filename (str): The filename
        full_path (str): The full path+filename
        headers (dict): The headers for each extension
        id (str): The GBM file ID
        is_gbm_file (bool): True if the file is a valid GBM standard file, 
                            False if it is not.
        is_trigger (bool): True if the file is a GBM trigger file, False if not
        npix (int): Number of pixels in the HEALPix map
        nside (int): The HEALPix resolution
        pixel_area (float): The area of each pixel in square degrees
        trigtime (float): The time corresponding to the localization
    """

    def __init__(self):
        self._headers = OrderedDict()
        self._prob = np.array([], dtype=float)
        self._sig = np.array([], dtype=float)
        super().__init__()

    @property
    def headers(self):
        return self._headers

    @property
    def trigtime(self):
        try:
            return self._headers['PRIMARY']['TRIGTIME']
        except:
            return None

    @property
    def npix(self):
        return len(self._prob)

    @property
    def nside(self):
        return hp.npix2nside(self.npix)

    @property
    def pixel_area(self):
        return 4.0 * 180.0 ** 2 / (np.pi * self.npix)

    @property
    def centroid(self):
        pix = np.argmax(self._prob)
        theta, phi = hp.pix2ang(self.nside, pix)
        return (self._phi_to_ra(phi), self._theta_to_dec(theta))

    @classmethod
    def from_data(cls, prob_arr, sig_arr, trigtime=None):
        """Create a HealPix object from healpix arrays
        
        Args:
            prob_arr (np.array): The HEALPix array containing the probability/pixel
            sig_arr (np.array): The HEALPix array containing the signficance
            trigtime (float, optional): The time corresponding to the localization
        
        Returns:        
            :class:`HealPix`: The HEALPix localization
        """
        obj = cls()
        obj._prob = obj._assert_prob(prob_arr)
        obj._sig = obj._assert_sig(sig_arr)

        # set file properties
        if trigtime is None:
            trigtime = 0.0
        obj.set_properties(trigtime=trigtime, datatype='healpix',
                           extension='fit')
        return obj

    @classmethod
    def from_annulus(cls, center_ra, center_dec, radius, sigma, nside=None,
                     **kwargs):
        """Create a HealPix object of a Gaussian-width annulus
        
        Args:
            center_ra (float): The RA of the center of the annulus
            center_dec (float): The Dec of the center of the annulus
            radius (float): The radius of the annulus, in degrees, measured to 
                            the center of the of the annulus
            sigma (float): The Gaussian standard deviation width of the annulus, 
                           in degrees
            nside (int, optional): The nside of the HEALPix to make. By default,
                                   the nside is automatically determined by the 
                                   `sigma` width.  Set this argument to 
                                   override the default. 
            
            **kwargs: Options to pass to :meth:`from_data`
        
        Return:
            :class:`HealPix`: The HEALPix annulus
        """
        
        # Automatically calculate appropriate nside by taking the closest nside
        # with an average resolution that matches 0.2*sigma
        if nside is None:
            nsides = 2**np.arange(15)
            pix_res = hp.nside2resol(nsides, True)/60.0
            idx = np.abs(pix_res-sigma/5.0).argmin()
            nside = nsides[idx]
        
        # get everything in the right units
        center_phi = cls._ra_to_phi(center_ra)
        center_theta = cls._dec_to_theta(center_dec)
        radius_rad = np.deg2rad(radius)
        sigma_rad = np.deg2rad(sigma)

        # number of points in the circle based on the approximate arclength 
        # and resolution
        res = hp.nside2resol(nside)
        
        # calculate normal distribution about annulus radius with sigma width
        x = np.linspace(0.0, np.pi, int(10.0*np.pi/res))
        pdf = norm.pdf(x, loc=radius_rad, scale=sigma_rad)

        # cycle through annuli of radii from 0 to 180 degree with the 
        # appropriate amplitude and fill the probability map
        probmap = np.zeros(hp.nside2npix(nside))
        for i in range(x.size):
            # no need to waste time on pixels that will have ~0 probability...
            if pdf[i]/pdf.max() < 1e-10:
                continue
            
            # approximate arclength determines number of points in each annulus
            arclength = 2.0*np.pi*x[i]
            numpts = int(np.ceil(arclength/res))*10
            circ = make_circle_poly(x[i], center_theta, center_phi, numpts)
            theta = np.pi / 2.0 - circ[:, 1]
            phi = circ[:, 0]
            
            # convert to pixel indixes and fill the map
            idx = hp.ang2pix(nside, theta, phi)
            probmap[idx] = pdf[i]
            mask = (probmap[idx] > 0.0)
            probmap[idx[~mask]] = pdf[i]
            probmap[idx[mask]] = (probmap[idx[mask]] + pdf[i])/2.0
        probmap /= probmap.sum()

        # signficance map
        sigmap = 1.0 - find_greedy_credible_levels(probmap)

        obj = cls.from_data(probmap, sigmap, **kwargs)
        return obj

    @classmethod
    def from_gaussian(cls, center_ra, center_dec, sigma, nside=None, **kwargs):
        """Create a HealPix object of a Gaussian
        
        Args:
            center_ra (float): The RA of the center of the Gaussian
            center_dec (float): The Dec of the center of the Gaussian
            sigma (float): The Gaussian standard deviation, in degrees
            nside (int, optional): The nside of the HEALPix to make. By default,
                                   the nside is automatically determined by the 
                                   `sigma` of the Gaussian.  Set this argument 
                                   to override the default. 
            **kwargs: Options to pass to :meth:`from_data`
        
        Returns:
            :class:`HealPix`: The HEALPix Gaussian
        """

        # Automatically calculate appropriate nside by taking the closest nside
        # with an average resolution that matches 0.2*sigma
        if nside is None:
            nsides = 2**np.arange(15)
            pix_res = hp.nside2resol(nsides, True)/60.0
            idx = np.abs(pix_res-sigma/10.0).argmin()
            nside = nsides[idx]
        
        # get everything in the right units
        center_phi = cls._ra_to_phi(center_ra)
        center_theta = cls._dec_to_theta(center_dec)
        sigma_rad = np.deg2rad(sigma)

        # point probability
        npix = hp.nside2npix(nside)
        probmap = np.zeros(npix)
        probmap[hp.ang2pix(nside, center_theta, center_phi)] = 1.0

        # then smooth out using appropriate gaussian kernel
        probmap = hp.smoothing(probmap, sigma=sigma_rad, verbose=False)

        # significance map
        sigmap = 1.0 - find_greedy_credible_levels(probmap)

        obj = cls.from_data(probmap, sigmap, **kwargs)
        return obj

    @classmethod
    def from_vertices(cls, ra_pts, dec_pts, nside=64, **kwargs):
        """Create a HealPix object from a list of RA, Dec vertices.
        The probability within the vertices will be distributed uniformly and
        zero probability outside the vertices.
        
        Args:
            ra_pts (np.array): The array of RA coordinates
            dec_pts (np.array): The array of Dec coordinates
            nside (int, optional): The nside of the HEALPix to make. Default is 64.
            **kwargs: Options to pass to :meth:`from_data`
        
        Returns:
            :class:`HealPix`: The HEALPix object
        """
        poly = Polygon(np.vstack((ra_pts, dec_pts)).T, closed=True)

        npix = hp.nside2npix(nside)
        theta, phi = hp.pix2ang(nside, np.arange(npix))
        ra = cls._phi_to_ra(phi)
        dec = cls._theta_to_dec(theta)
        mask = poly.contains_points(np.vstack((ra, dec)).T)

        probmap = np.zeros(npix)
        probmap[mask] = 1.0
        probmap /= probmap.sum()

        # significance map
        sigmap = 1.0 - find_greedy_credible_levels(probmap)

        obj = cls.from_data(probmap, sigmap, **kwargs)
        return obj

    @classmethod
    def multiply(cls, healpix1, healpix2, primary=1, output_nside=128):
        """Multiply two HealPix maps and return a new HealPix object
        
        Args:
            healpix1 (:class:`HealPix`): One of the HEALPix maps to multiply
            healpix2 (:class:`HealPix`): The other HEALPix map to multiply
            primary (int, optional): If 1, use the first map header information, 
                                     or if 2, use the second map header 
                                     information. Default is 1.
            output_nside (int, optional): The nside of the multiplied map. 
                                          Default is 128.
        Returns
            :class:`HealPix`: The multiplied map
        """
        # if different resolutions, upgrade the lower res, then multiply
        if healpix1.nside > healpix2.nside:
            prob = healpix1._prob * hp.ud_grade(healpix2._prob,
                                                nside_out=healpix1.nside)
        elif healpix1.nside < healpix2.nside:
            prob = healpix2._prob * hp.ud_grade(healpix1._prob,
                                                nside_out=healpix2.nside)
        else:
            prob = healpix1._prob * healpix2._prob

        # output resolution and normalize
        prob = hp.ud_grade(prob, output_nside)
        prob = prob / np.sum(prob)
        sig = 1.0 - find_greedy_credible_levels(prob)

        # copy header info
        if primary == 1:
            headers = healpix1.headers
            trigtime = healpix1.trigtime
        else:
            headers = healpix2.headers
            trigtime = healpix2.trigtime
        
        if 'HEALPIX' in headers:
            headers['HEALPIX']['NSIDE'] = output_nside

        obj = cls.from_data(prob, sig, trigtime=trigtime)
        obj._headers = headers
        return obj

    def probability(self, ra, dec, per_pixel=False):
        """Calculate the localization probability at a given point.  This
        function interpolates the map at the requested point rather than
        providing the vale at the nearest pixel center.
        
        Args:
            ra (float): The RA
            dec (float): The Dec
            per_pixel (bool, optional): 
                If True, return probability per pixel, otherwise return 
                probability per square degree. Default is False.
        
        Returns:
            float: The localization probability
        """
        phi = self._ra_to_phi(ra)
        theta = self._dec_to_theta(dec)
        prob = hp.get_interp_val(self._prob, theta, phi)
        if not per_pixel:
            prob /= self.pixel_area
        return prob

    def confidence(self, ra, dec):
        """Calculate the localization confidence level for a given point. 
        This function interpolates the map at the requested point rather than
        providing the value at the nearest pixel center.
        
        Args:
            ra (float): The RA
            dec (float): The Dec
        
        Returns:
            float: The localization confidence level
        """
        phi = self._ra_to_phi(ra)
        theta = self._dec_to_theta(dec)
        return 1.0 - hp.get_interp_val(self._sig, theta, phi)

    def area(self, clevel):
        """Calculate the sky area contained within a given confidence region
        
        Args:
            clevel (float): The localization confidence level (valid range 0-1)
        
        Returns:
            float: The area contained in square degrees
        """
        numpix = np.sum((1.0 - self._sig) <= clevel)
        return numpix * self.pixel_area

    def prob_array(self, numpts_ra=360, numpts_dec=180, sqdegrees=True,
                   sig=False):
        """Return the localization probability mapped to a grid on the sky
        
        Args:
            numpts_ra (int, optional): The number of grid points along the RA 
                                       axis. Default is 360.
            numpts_dec (int, optional): The number of grid points along the Dec 
                                        axis. Default is 180.
            sqdegrees (bool, optional): 
                If True, the prob_array is in units of probability per square 
                degrees, otherwise in units of probability per pixel. 
                Default is True
            sig (bool, optional): Set True to retun the significance map on a 
                                  grid instead of the probability. Default is False.

        Returns: 
            3-tuple containing:
            
            - *np.array*: The probability (or significance) array with shape \
                      (``numpts_dec``, ``numpts_ra``)
            - *np.array*: The RA grid points
            - *np.array*: The Dec grid points
        """
        grid_pix, phi, theta = self._mesh_grid(numpts_ra, numpts_dec)

        if sig:
            sqdegrees = False
            prob_arr = self._sig[grid_pix]
        else:
            prob_arr = self._prob[grid_pix]
        if sqdegrees:
            prob_arr /= self.pixel_area
        return (prob_arr, self._phi_to_ra(phi), self._theta_to_dec(theta))

    def confidence_region_path(self, clevel, numpts_ra=360, numpts_dec=180):
        """Return the bounding path for a given confidence region
        
        Args:
            clevel (float): The localization confidence level (valid range 0-1)
            numpts_ra (int, optional): The number of grid points along the RA 
                                       axis. Default is 360.
            numpts_dec (int, optional): The number of grid points along the Dec 
                                        axis. Default is 180.
        
        Returns:
            [(np.array, np.array), ...]: A list of RA, Dec points, where each \
                item in the list is a continuous closed path.
        """
        # create the grid and integrated probability array
        grid_pix, phi, theta = self._mesh_grid(numpts_ra, numpts_dec)
        sig_arr = 1.0 - self._sig[grid_pix]
        ra = self._phi_to_ra(phi)
        dec = self._theta_to_dec(theta)

        # use matplotlib contour to produce a path object
        contour = Contour(ra, dec, sig_arr, [clevel])

        # get the contour path, which is made up of segments
        paths = contour.collections[0].get_paths()

        # extract all the vertices
        pts = [path.vertices for path in paths]

        # unfortunately matplotlib will plot this, so we need to remove
        for c in contour.collections:
            c.remove()

        return pts

    def source_probability(self, ra, dec, prior=0.5):
        r"""The probability that the HealPix localization is associated with
        a known point location.  This is calculated against the null hypothesis
        that the HealPix localization originates from an unassociated random
        source that has equal probability of origination anywhere in the sky:
        
        :math:`P(A | \mathcal{I}) = 
        \frac{P(\mathcal{I} | A) \ P(A)}
        {P(\mathcal{I} | A) \ P(A) + P(\mathcal{I} | \neg A) \ P(\neg A)}`
        
        where
        
        * :math:`P(\mathcal{I} | A)` is the probability of the localization at
          the point source once
        * :math:`P(\mathcal{I} | \neg A)` is the probability per pixel assuming 
          a uniform distribution on the sky (i.e. the probability the 
          localization is associated with a random point on the sky)
        * :math:`P(A)` is the prior probability that the localization is 
          associated with the point source
        
        Args:
            ra (float): The RA of the known source location
            dec (float): The Dec of the known source location
            prior (float, optional): The prior probability that the localization
                                     is associated with the source. 
                                     Default is 0.5
        
        Returns:        
            float: The probability that the HealPix localization is spatially \
            associated with the point source
        """
        if (prior < 0.0) or (prior > 1.0):
            raise ValueError('Prior probability must be within 0-1, inclusive')
        # convert uniform prob/sr to prob/pixel
        u = 1.0 / (4.0 * np.pi)
        u *= hp.nside2resol(self.nside) ** 2

        # the pixel probability of the skymap at the location of the point source
        p = self.probability(ra, dec, per_pixel=True)

        # null hypothesis is that they are not associated, therefore the sky map
        # is result of some source that has uniform probability on the sky
        prob = (p*prior) / ((p*prior) + (u*(1.0-prior)))
        return prob

    def region_probability(self, healpix, prior=0.5):
        r"""The probability that the HealPix localization is associated with
        another HealPix map.  This is calculated against the null hypothesis
        that the two HealPix maps are unassociated:
        
        :math:`P(A | \mathcal{I}) = 
        \frac{P(\mathcal{I} | A) \ P(A)}
        {P(\mathcal{I} | A) \ P(A) + P(\mathcal{I} | \neg A) \ P(\neg A)}`
        
        where
        
        * :math:`P(\mathcal{I} | A)` is the integral over the overlap of the two 
          maps once the Earth occultation has been removed for *this* map.
        * :math:`P(\mathcal{I} | \neg A)` is the integral over the overlap of
          *this* map with a uniform distribution on the sky (i.e. the probability 
          the localization is associated with a random point on the sky)
        * :math:`P(A)` is the prior probability that *this* localization is 
          associated with the *other* HEALPix map.
        
        Args:
            healpix (:class:`HealPix`): The healpix map for which to calculate 
                                        the spatial association
            prior (float, optional): The prior probability that the localization
                                     is associated with the source. 
                                     Default is 0.5
        
        Returns:  
            float: The probability that this HealPix localization is
            associated with the input HealPix map
        """
        if (prior < 0.0) or (prior > 1.0):
            raise ValueError('Prior probability must be within 0-1, inclusive')
        # convert uniform prob/sr to prob/pixel
        u = 1.0 / (4.0 * np.pi)

        # ensure maps are the same resolution
        probmap1 = self._prob
        probmap2 = healpix._prob
        if self.nside > healpix.nside:
            probmap2 = hp.ud_grade(probmap2, nside_out=self.nside)
            probmap2 = self._assert_prob(probmap2)
            u *= hp.nside2resol(self.nside) ** 2
        elif self.nside < healpix.nside:
            probmap1 = hp.ud_grade(probmap1, nside_out=healpix.nside)
            probmap1 = self._assert_prob(probmap1)
            u *= hp.nside2resol(healpix.nside) ** 2
        else:
            u *= hp.nside2resol(self.nside) ** 2

        # alternative hypothesis: they are related
        alt_hyp = np.sum(probmap1 * probmap2)
        # null hypothesis: one of the maps is from an unassociated source
        # (uniform spatial probability)
        null_hyp = np.sum(probmap1 * u)

        # since we have an exhaustive and complete list of possibilities, we can
        # easily calculate the probability
        prob = (alt_hyp*prior) / ((alt_hyp*prior) + (null_hyp*(1.0-prior)))
        return prob

    def convolve(self, model, *args):
        """Convolve the map with a model kernel.  The model can be a Gaussian
        kernel or any mixture of Gaussian kernels. Uses `healpy.smoothing 
        <https://healpy.readthedocs.io/en/latest/generated/healpy.sphtfunc.smoothing.html>`_.

        An example of a model kernel with a 50%/50% mixture of two Gaussians,
        one with a 1-deg width, and the other with a 3-deg width::
            def gauss_mix_example():
                sigma1 = np.deg2rad(1.0)
                sigma2 = np.deg2rad(3.0)
                frac1 = 0.50
                return ([sigma1, sigma2], [frac1])
        
        Args: 
            model (<function>): The function representing the model kernel
            *args: Arguments to be passed to the model kernel function
        
        Returns:
            :class:`HealPix`: A new HealPix object that is a result of the \
                              convolution with the model kernel
        """
        # evaluate model
        sigmas, fracs = model(*args)

        # determine number of gaussians, and ensure that they match the 
        # number of fractional weights
        num_sigmas = len(sigmas)
        if len(fracs) != num_sigmas:
            if len(fracs) + 1 != num_sigmas:
                raise ValueError(
                    'Number of mixture fraction parameters is incorrect')
            fracs.append(1.0 - np.sum(fracs))

        # for each gaussian, apply the smoothing at the prescribed weight
        new_prob = np.zeros(self._prob.shape)
        for i in range(num_sigmas):
            new_prob += fracs[i] * hp.smoothing(self._prob, sigma=sigmas[i],
                                                verbose=False)

        # make the object
        new_sig = 1.0 - find_greedy_credible_levels(new_prob)
        new_obj = deepcopy(self)
        new_obj._prob = new_obj._assert_prob(new_prob)
        new_obj._sig = new_obj._assert_sig(new_sig)
        return new_obj

    @staticmethod
    def _ra_to_phi(ra):
        return np.deg2rad(ra)

    @staticmethod
    def _phi_to_ra(phi):
        return np.rad2deg(phi)

    @staticmethod
    def _dec_to_theta(dec):
        return np.deg2rad(90.0 - dec)

    @staticmethod
    def _theta_to_dec(theta):
        return np.rad2deg(np.pi / 2.0 - theta)

    def _ang_to_pix(self, ra, dec):
        # convert RA/Dec to healpixels
        theta = self._dec_to_theta(dec)
        phi = self._ra_to_phi(ra)
        pix = hp.ang2pix(self.nside, theta, phi)
        return pix

    def _mesh_grid(self, num_phi, num_theta):
        # create the mesh grid in phi and theta
        theta = np.linspace(np.pi, 0.0, num_theta)
        phi = np.linspace(0.0, 2 * np.pi, num_phi)
        phi_grid, theta_grid = np.meshgrid(phi, theta)
        grid_pix = hp.ang2pix(self.nside, theta_grid, phi_grid)
        return (grid_pix, phi, theta)

    def _assert_prob(self, prob):
        # ensure that the pixels have valid probability:
        # each pixel must be > 0 and sum == 1.
        prob[prob < 0.0] = 0.0
        prob /= prob.sum()
        return prob

    def _assert_sig(self, sig):
        # ensure that the pixels have valid significance:
        # each pixel must have significance [0, 1]
        if sig is not None:
            sig[sig < 0.0] = 0.0
            sig[sig > 1.0] = 1.0
        return sig


class GbmHealPix(HealPix):
    """Class for GBM HEALPix localization files.
    
    Attributes:
        <detector_name>_pointing (float, float):
            The RA, Dec of the detector pointing (e.g. ``GbmHealPix.n0_pointing``)    
        centroid (float, float): The RA and Dec of the highest probability pixel
        datatype (str): The datatype of the file
        detector (str): The GBM detector the file is associated with
        directory (str): The directory the file is located in
        filename (str): The filename
        full_path (str): The full path+filename
        geo_location (float, float): The geocenter RA, Dec at trigtime
        geo_probability (float): The amount of localization probability on the Earth
        geo_radius (float): The apparent Earth radius as observed by Fermi
        headers (dict): The headers for each extension
        id (str): The GBM file ID
        is_gbm_file (bool): True if the file is a valid GBM standard file, 
                            False if it is not.
        is_trigger (bool): True if the file is a GBM trigger file, False if not
        npix (int): Number of pixels in the HEALPix map
        nside (int): The HEALPix resolution
        quaternion (np.array): The spacecraft attitude quaternion
        pixel_area (float): The area of each pixel in square degrees
        scpos (np.array): The spacecraft position in Earth inertial coordinates
        sun_location (float, float): The Sun RA, Dec at trigtime
        trigtime (float): The time corresponding to the localization
    """

    def __init__(self):
        super().__init__()

    @property
    def sun_location(self):
        try:
            return (self._headers['HEALPIX']['SUN_RA'],
                    self._headers['HEALPIX']['SUN_DEC'])
        except:
            return None

    @property
    def geo_location(self):
        try:
            return (self._headers['HEALPIX']['GEO_RA'],
                    self._headers['HEALPIX']['GEO_DEC'])
        except:
            return None

    @property
    def geo_radius(self):
        # if the radius isn't known, use the average 67.5 deg radius
        try:
            return self._headers['HEALPIX']['GEO_RAD']
        except:
            return 67.5

    @property
    def scpos(self):
        if 'COMMENT' not in self.headers['HEALPIX']:
            return None
        scpos = [c for c in self.headers['HEALPIX']['COMMENT'] if 'SCPOS' in c]
        if len(scpos) != 1:
            return None
        else:
            scpos = scpos[0].split('[')[1].split(']')[0]
            scpos = np.array([float(el) for el in scpos.split()])
        return scpos

    @property
    def quaternion(self):
        if 'COMMENT' not in self.headers['HEALPIX']:
            return None
        quat = [c for c in self.headers['HEALPIX']['COMMENT'] if 'QUAT' in c]
        if len(quat) != 1:
            return None
        else:
            quat = quat[0].split('[')[1].split(']')[0]
            quat = np.array([float(el) for el in quat.split()])
        return quat

    @property
    def geo_probability(self):
        if self.geo_location is None:
            return None
        prob_mask, geo_mask = self._earth_mask()
        return np.sum(self._prob[prob_mask][geo_mask])

    @classmethod
    def open(cls, filename):
        """Open a GBM HEALPix FITS file and return the GbmHealPix object
        
        Args:
            filename (str): The filename of the FITS file
        
        Returns:        
            :class:`GbmHealPix`: The GBM HEALPix localization
        """
        warnings.filterwarnings("ignore", category=UserWarning)
        
        obj = cls()
        obj._file_properties(filename)

        # open FITS file
        with fits.open(filename, mmap=False) as hdulist:
            for hdu in hdulist:
                obj._headers.update({hdu.name: hdu.header})
        # the healpix arrays
        prob, sig = hp.read_map(filename, field=(0, 1), memmap=False,
                                verbose=False)
        obj._prob = obj._assert_prob(prob)
        obj._sig = obj._assert_sig(sig)

        # set the detector pointing attributes
        try:
            obj._set_det_attr()
        except:
            pass

        return obj

    @classmethod
    def from_data(cls, prob_arr, sig_arr, tcat=None, trigtime=None,
                  quaternion=None, scpos=None):
        """Create a HealPix object from healpix arrays and optional metadata

        Args:
            prob_arr (np.array): The HEALPix array containing the probability/pixel
            sig_arr (np.array): The HEALPix array containing the signficance
            tcat (:class:`.Tcat`, optional): The associated Tcat to fill out 
                                             the primary header info
            trigtime (float, optional): The time corresponding to the localization
            quaternion (np.array, optional): 
                The associated spacecraft quaternion used to determine the 
                detector pointings in equatorial coordinates
            scpos (np.array, optional): 
                The associated spacecraft position in Earth inertial coordinates 
                used to determine the geocenter location in equatorial coordinates
            
        Returns:        
            :class:`GbmHealPix`: The HEALPix localization
        """
        obj = cls()
        obj._prob = obj._assert_prob(prob_arr)
        obj._sig = obj._assert_sig(sig_arr)

        if tcat is not None:
            trigtime = tcat.trigtime
        if trigtime is None:
            trigtime = 0.0

        comments = []

        # if we have a trigtime, calculate sun position
        sun_key = []
        if trigtime is not None:
            sun_loc = get_sun_loc(trigtime)
            sun_key = [('SUN_RA', sun_loc[0], 'RA of Sun'),
                       ('SUN_DEC', sun_loc[1], 'Dec of Sun')]

        # if we have a scpos, calculate geocenter position, radius
        geo_key = []
        if scpos is not None:
            comments.append(('COMMENT', 'SCPOS: ' + np.array2string(scpos)))
            geo = geocenter_in_radec(scpos)
            try:
                _, alt = latitude_from_geocentric_coords_complex(scpos)
            except:
                warn('Using simple spheroidal Earth approximation')
                _, alt = latitude_from_geocentric_coords_simple(scpos)
            r = 6371.0 * 1000.0
            geo_radius = np.rad2deg(np.arcsin(r / (r + alt)))
            geo_key = [
                ('GEO_RA', float(geo[0]), 'RA of Geocenter relative to Fermi'),
                ('GEO_DEC', float(geo[1]),
                 'Dec of Geocenter relative to Fermi'),
                ('GEO_RAD', geo_radius, 'Radius of the Earth')]

        # if we have a quaternion, calculate detector pointings
        det_keys = []
        if quaternion is not None:
            comments.append(
                ('COMMENT', 'QUAT: ' + np.array2string(quaternion)))
            keys = []
            for det in Detector:
                detname = det.short_name
                ra, dec = spacecraft_to_radec(det.azimuth, det.zenith,
                                              quaternion)
                ra_key = (detname + '_RA', float(ra),
                          'RA pointing for detector ' + detname)
                dec_key = (detname + '_DEC', float(dec),
                           'Dec pointing for detector ' + detname)
                keys.append([ra_key, dec_key])
            det_keys = [key for det in keys for key in det]

        # put the additional keys together, and create the headers
        keys = sun_key
        keys.extend(geo_key)
        keys.extend(det_keys)
        keys.extend(comments)
        prihdr = healpix_primary(tcat=tcat, trigtime=trigtime)
        obj._headers['PRIMARY'] = prihdr
        obj._headers['HEALPIX'] = healpix_image(nside=obj.nside,
                                                extra_keys=keys,
                                                object=prihdr['OBJECT'])

        # set the detector pointing attributes
        try:
            obj._set_det_attr()
        except:
            pass

        # set file properties
        obj.set_properties(trigtime=obj.trigtime, datatype='healpix',
                           extension='fit')
        return obj

    @classmethod
    def from_chi2grid(cls, chi2grid, nside=128, tcat=None):
        """Create a GbmHealPix object from a chi2grid object
        
        Args:
            chi2grid (class:`Chi2Grid`): The chi2grid object containing the 
                                         chi-squared/log-likelihood info
            nside (int, optional): The nside resolution to use. Default is 128
            tcat (:class:`.Tcat`, optional): The associated Tcat to fill out 
                                             the primary header info
        
        Returns:        
            :class:`GbmHealPix`: The GBM HEALPix localization
        """
        # fill up a low-resolution healpix map with significance
        lores_nside = 64
        lores_npix = hp.nside2npix(lores_nside)
        lores_array = np.zeros((lores_npix))
        theta = cls._dec_to_theta(chi2grid.dec)
        phi = cls._ra_to_phi(chi2grid.ra)
        idx = hp.ang2pix(lores_nside, theta, phi)
        lores_array[idx] = chi2grid.significance

        # upscale to high-resolution
        hires_nside = nside
        hires_npix = hp.nside2npix(hires_nside)
        theta, phi = hp.pix2ang(hires_nside, np.arange(hires_npix))
        sig_array = hp.get_interp_val(lores_array, theta, phi)
        sig_array[sig_array < 0.0] = 0.0

        # convert chisq map to probability map
        loglike = -chi2grid.chisq / 2.0
        probs = np.exp(loglike - np.max(loglike))
        lores_array = np.zeros(lores_npix)
        lores_array[idx] = probs
        prob_array = hp.get_interp_val(lores_array, theta, phi)
        prob_array[prob_array < 0.0] = 0.0
        prob_array /= np.sum(prob_array)

        obj = cls.from_data(prob_array, sig_array, tcat=tcat,
                            trigtime=chi2grid.trigtime, scpos=chi2grid.scpos,
                            quaternion=chi2grid.quaternion)
        return obj

    @classmethod
    def multiply(cls, healpix1, healpix2, primary=1, output_nside=128):
        """Multiply two GbmHealPix maps and return a new GbmHealPix object
        
        Note:
            Either `healpix1` *or* healpix2 can be a non-GbmHealPix object, 
            however at least one of them must be a GbmHealPix object **and**
            the `primary` argument must be set to the appropriate GbmHealPix
            object otherwise a TypeError will be raised.

        Args:
            healpix1 (:class:`HealPix` or :class:`GbmHealPix`): 
                One of the HEALPix maps to multiply
            healpix2 (:class:`HealPix` or :class:`GbmHealPix`): 
                The other HEALPix map to multiply
            primary (int, optional): If 1, use the first map header information, 
                                     or if 2, use the second map header 
                                     information. Default is 1.
            output_nside (int, optional): The nside of the multiplied map. 
                                          Default is 128.
        Returns
            :class:`GbmHealPix`: The multiplied map
        """

        if primary == 1:
            if not isinstance(healpix1, cls):
                 raise TypeError('Primary HealPix (healpix1) is not of class {}. '
                'Perhaps try setting healpix2 as the primary'.format(cls.__name__))
        else:
            if not isinstance(healpix2, cls):
                raise TypeError('Primary HealPix (healpix2) is not of class {}. '
                'Perhaps try setting healpix1 as the primary'.format(cls.__name__))
        
        obj = super().multiply(healpix1, healpix2, primary=primary, 
                               output_nside=output_nside)
        obj._set_det_attr()

        return obj 
    
    def write(self, directory, filename=None):
        """Write the GbmHealPix object to a FITS file
        
        Args:
            directory (str): The directory to write to
            filename (str, optional): The filename of the FITS file
        """
        if filename is None:
            filename = self.filename
        self.headers['PRIMARY']['FILENAME'] = filename
        out_file = os.path.join(directory, filename)

        # get arrays in proper order, and write the healpix data to disk
        prob_arr = hp.reorder(self._prob, r2n=True)
        sig_arr = hp.reorder(self._sig, r2n=True)
        columns = ['PROBABILITY', 'SIGNIFICANCE']
        hp.write_map(out_file, (prob_arr, sig_arr), nest=True, coord='C',
                     overwrite=True, \
                     column_names=columns,
                     extra_header=self.headers['HEALPIX'].cards)

        # healpy doesn't allow direct input into the primary header on writing,
        # so we have to open the written file, add the primary header, rename
        # the tables in the HEALPIX extension and write a new file
        hdulist = fits.open(out_file)
        hdulist[0].header.extend(self.headers['PRIMARY'])
        hdulist[1].name = 'HEALPIX'
        hdulist[1].header['TTYPE1'] = (
        'PROBABILITY', 'Differential probability per pixel')
        hdulist[1].header['TTYPE2'] = (
        'SIGNIFICANCE', 'Integrated probability')
        hdulist.writeto(out_file, clobber=True, checksum=True)
    
    @classmethod
    def remove_earth(cls, healpix):
        """Return a new GbmHealPix with the probability on the Earth masked out.
        The remaining probability on the sky is renormalized.

        Note:
            The :attr:`geo_location` attribute must be available to use this function

        Args:
            healpix (:class:`GbmHealPix`): The map for which the Earth will be 
                                           removed        
        
        Returns: 
            :class:`GbmHealPix`: GBM HEALPix localization
        """
        if healpix.geo_location is None:
            raise ValueError('Location of geocenter is not known')

        # get the non-zero probability and earth masks
        prob_mask, geo_mask = healpix._earth_mask()

        # zero out the probabilities behind the earth
        new_prob = np.copy(healpix._prob)
        temp = new_prob[prob_mask]
        temp[geo_mask] = 0.0
        new_prob[prob_mask] = temp
        # renormalize
        new_prob /= np.sum(new_prob)
        # have to redo the significance
        new_sig = 1.0 - find_greedy_credible_levels(new_prob)

        # return a new object
        obj = cls()
        obj._prob = obj._assert_prob(new_prob)
        obj._sig = obj._assert_sig(new_sig)
        obj._headers = healpix.headers
        # set the detector pointing attributes
        try:
            obj._set_det_attr()
        except:
            pass

        # set file properties
        obj.set_properties(trigtime=obj.trigtime, datatype='healpix',
                           extension='fit')
        return obj

    def source_probability(self, ra, dec, prior=0.5):
        r"""The probability that the GbmHealPix localization is associated with
        a known point location.  This is calculated against the null hypothesis
        that the localization originates from an unassociated random source 
        that has equal probability of origination anywhere in the sky: 
        
        :math:`P(A | \mathcal{I}) = 
        \frac{P(\mathcal{I} | A) \ P(A)}
        {P(\mathcal{I} | A) \ P(A) + P(\mathcal{I} | \neg A) \ P(\neg A)}`
        
        where
        
        * :math:`P(\mathcal{I} | A)` is the probability of the localization at
          the point source once the Earth occultation has been removed
        * :math:`P(\mathcal{I} | \neg A)` is the probability per pixel assuming 
          a uniform distribution on the sky (i.e. the probability the 
          localization is associated with a random point on the sky)
        * :math:`P(A)` is the prior probability that the localization is 
          associated with the point source
        
        Note: 
            If the point source is behind the Earth, then it is assumed that
            GBM could not observe it, therefore the probability will be zero. 
        
        Args:
            ra (float): The RA of the known source location
            dec (float): The Dec of the known source location
            prior (float, optional): The prior probability that the localization
                                     is associated with the source. 
                                     Default is 0.5
        
        Returns:        
            float: The probability that the localization is spatially
            associated with the point source
        """
        if (prior < 0.0) or (prior > 1.0):
            raise ValueError('Prior probability must be within 0-1, inclusive')
        
        # convert uniform prob/sr to prob/pixel
        u = 1.0 / (4.0 * np.pi)
        u *= hp.nside2resol(self.nside) ** 2

        # the pixel probability of the skymap at the location of the point source
        p = type(self).remove_earth(self).probability(ra, dec, per_pixel=True)
        # if we know the location of the earth and it's behind the earth,
        # then we obviously couldn't have seen it
        if self.geo_location is not None:
            ang = haversine(*self.geo_location, ra, dec)
            if ang < self.geo_radius:
                p = 0.0

        # null hypothesis is that they are not associated, therefore the sky map
        # is result of some source that has uniform probability on the sky
        prob = (p*prior) / ((p*prior) + (u*(1.0-prior)))
        return prob

    def region_probability(self, healpix, prior=0.5):
        r"""The probability that the localization is associated with
        the localization region from another map.  This is calculated 
        against the null hypothesis that the two maps represent 
        unassociated sources:
        
        :math:`P(A | \mathcal{I}) = 
        \frac{P(\mathcal{I} | A) \ P(A)}
        {P(\mathcal{I} | A) \ P(A) + P(\mathcal{I} | \neg A) \ P(\neg A)}`
        
        where
        
        * :math:`P(\mathcal{I} | A)` is the integral over the overlap of the two 
          maps once the Earth occultation has been removed for *this* map.
        * :math:`P(\mathcal{I} | \neg A)` is the integral over the overlap of
          *this* map with a uniform distribution on the sky (i.e. the probability 
          the localization is associated with a random point on the sky)
        * :math:`P(A)` is the prior probability that *this* localization is 
          associated with the *other* HEALPix map.

        Note: 
            The localization region of *this* map overlapping the Earth will be
            removed and the remaining unocculted region is used for the
            calculation.  The *other* map is assumed to have no exclusionary
            region.
        
        Args:
            healpix (:class:`HealPix`): The healpix map for which to calculate 
                                        the spatial association
            prior (float, optional): The prior probability that the localization
                                     is associated with the source. 
                                     Default is 0.5
        
        Returns:  
            float: The probability that the two HEALPix maps are associated.
        """
        if (prior < 0.0) or (prior > 1.0):
            raise ValueError('Prior probability must be within 0-1, inclusive')

        # convert uniform prob/sr to prob/pixel
        u = 1.0 / (4.0 * np.pi)

        # get the non-zero probability and earth masks
        prob_mask, geo_mask = self._earth_mask()
        probmap1 = np.copy(self._prob)
        temp = probmap1[prob_mask]
        temp[geo_mask] = 0.0
        probmap1[prob_mask] = temp
        probmap1 /= np.sum(probmap1)

        # ensure maps are the same resolution and convert uniform prob/sr to 
        # prob/pixel
        probmap2 = np.copy(healpix._prob)
        if self.nside > healpix.nside:
            probmap2 = hp.ud_grade(probmap2, nside_out=self.nside)
            probmap2 = self._assert_prob(probmap2)
            u *= hp.nside2resol(self.nside) ** 2
        elif self.nside < healpix.nside:
            probmap1 = hp.ud_grade(probmap1, nside_out=healpix.nside)
            probmap1 = self._assert_prob(probmap1)
            u *= hp.nside2resol(healpix.nside) ** 2
        else:
            u *= hp.nside2resol(self.nside) ** 2

        # alternative hypothesis: they are related
        alt_hyp = np.sum(probmap1 * probmap2)
        # null hypothesis: one of the maps is from an unassociated source
        # (uniform spatial probability)
        null_hyp = np.sum(probmap1 * u)

        # since we have an exhaustive and complete list of possibilities, we can
        # easily calculate the probability
        prob = (alt_hyp * prior) / ((alt_hyp*prior) + (null_hyp*(1.0-prior)))
        return prob

    def observable_fraction(self, healpix):
        """The observable fraction of a healpix probability region on the sky. 
        Non-observable regions are ones that are behind the Earth.
        
        Args:
            healpix (:class:`HealPix`): The healpix region for which to 
                                        calculate the observable fraction.
        Returns:        
            float: The fraction of the map (based on probability) that is observable.
        """
        # speed things up a bit by only considering pixels with non-zero prob
        prob_mask = (healpix._prob > 0.0)
        # get ra, dec coords for pixels and calculate angle from geocenter
        theta, phi = hp.pix2ang(healpix.nside, np.arange(healpix.npix))
        ra = self._phi_to_ra(phi)[prob_mask]
        dec = self._theta_to_dec(theta)[prob_mask]
        # the mask of everything with prob > 0.0 and is visible
        ang = haversine(*self.geo_location, ra, dec)
        geo_mask = (ang > self.geo_radius)

        # sum it up and divide by total prob (should be 1, but good to be sure)
        temp = np.copy(healpix._prob)
        temp = temp[prob_mask]
        frac = np.sum(temp[geo_mask]) / np.sum(healpix._prob)
        return frac

    def _set_det_attr(self):
        # set the detector pointing attributes
        keys = list(self.headers['HEALPIX'].keys())
        regex = re.compile('N._RA|B._RA')
        dets = [key.split('_')[0] for key in keys if re.match(regex, key)]
        for det in dets:
            setattr(self, det.lower() + '_pointing',
                    (self.headers['HEALPIX'][det + '_RA'],
                     self.headers['HEALPIX'][det + '_DEC']))

    def _earth_mask(self):
        # speed things up a bit by only considering pixels with non-zero prob
        mask = (self._prob > 0.0)
        # get ra, dec coords for pixels and calculate angle from geocenter
        theta, phi = hp.pix2ang(self.nside, np.arange(self.npix))
        ra = self._phi_to_ra(phi)[mask]
        dec = self._theta_to_dec(theta)[mask]
        ang = haversine(*self.geo_location, ra, dec)

        geo_radius = self.geo_radius
        # the mask of the non-zero probability pixels that are behind the earth
        geo_mask = (ang <= geo_radius)

        return mask, geo_mask


class Chi2Grid():
    """Class for the Chi2Grid localization files/objects
    
    Attributes:
        azimuth (np.array): The spacecraft azimuth grid points
        chisq (np.array): The chi-squared value at each grid point
        dec (np.array): The Dec grid points
        numpts (int): Number of sky points in the Chi2Grid
        quaternion (np.array): The spacecraft attitude quaternion
        ra (np.array): The RA grid points
        scpos (np.array): The spacecraft position in Earth inertial coordinates
        significance (np.array): The significance value at each point
        trigtime (float): The trigger time
        zenith (np.array): The spacecraft zenith grid points
        
    """

    def __init__(self):
        self._az = np.array([])
        self._zen = np.array([])
        self._ra = np.array([])
        self._dec = np.array([])
        self._chisq = np.array([])
        self._quaternion = None
        self._scpos = None
        self._trigtime = None

    @property
    def quaternion(self):
        return self._quaternion

    @quaternion.setter
    def quaternion(self, val):
        if len(val) != 4:
            raise ValueError('quaternion must be a 4-element array')
        self._quaternion = np.asarray(val)

    @property
    def scpos(self):
        return self._scpos

    @scpos.setter
    def scpos(self, val):
        if len(val) != 3:
            raise ValueError('scpos must be a 3-element array')
        self._scpos = np.asarray(val)

    @property
    def trigtime(self):
        return self._trigtime

    @trigtime.setter
    def trigtime(self, val):
        try:
            val = float(val)
        except:
            raise ValueError('trigtime must be a float')
        self._trigtime = val

    @property
    def numpts(self):
        return self._az.size

    @property
    def azimuth(self):
        return self._az

    @property
    def zenith(self):
        return self._zen

    @property
    def ra(self):
        return self._ra

    @property
    def dec(self):
        return self._dec

    @property
    def chisq(self):
        return self._chisq

    @property
    def significance(self):
        min_chisq = np.min(self.chisq)
        return 1.0 - chi2.cdf(self.chisq - min_chisq, 2)

    @classmethod
    def open(cls, filename):
        """Read a chi2grid file and create a Chi2Grid object
        
        Args:
            filename (str): The filename of the chi2grid file
        
        Returns:        
           :class:`Chi2Grid`: The Chi2Grid object
        """
        with open(filename, 'r') as f:
            txt = list(f)

        obj = cls()

        numpts = int(txt[0].strip())
        txt = txt[1:]
        obj._az = np.empty(numpts)
        obj._zen = np.empty(numpts)
        obj._ra = np.empty(numpts)
        obj._dec = np.empty(numpts)
        obj._chisq = np.empty(numpts)
        for i in range(numpts):
            line = txt[i].split()
            obj._az[i] = float(line[0].strip())
            obj._zen[i] = float(line[1].strip())
            obj._chisq[i] = float(line[2].strip())
            obj._ra[i] = float(line[4].strip())
            obj._dec[i] = float(line[5].strip())

        return obj

    @classmethod
    def from_data(cls, az, zen, ra, dec, chisq):
        """Create a Chi2Grid object from arrays
        
        Args:
            az (np.array): The azimuth grid points
            zen (np.array): The zenith grid points
            ra (np.array): The RA grid points
            dec (np.array): The Dec grid points
            chisq (np.array): The chi-squared values at each grid point
        
        Returns:        
            :class:`Chi2Grid`: The Chi2Grid object
        """
        obj = cls()
        obj._az = az
        obj._zen = zen
        obj._ra = ra
        obj._dec = dec
        obj._chisq = chisq
        return obj


def find_greedy_credible_levels(p):
    """Calculate the credible values of a probability array using a greedy
    algorithm.
    
    Args:
        p (np.array): The probability array
    
    Returns:    
         np.array: The credible values
    """
    p = np.asarray(p)
    pflat = p.ravel()
    i = np.argsort(pflat)[::-1]
    cs = np.cumsum(pflat[i])
    cls = np.empty_like(pflat)
    cls[i] = cs
    return cls.reshape(p.shape)


# Systematic Model definitions using healpy.smoothing
# --------------------------------------------------------
def GBUTS_Model_O3():
    """The localization systematic model for the targeted search during O3:
    a 2.7 deg Gaussian.
    
    References:
        arXiv:1903.12597
    """
    sigma = np.deg2rad(2.7)
    return ([sigma], [1.0])


def HitL_Model(az):
    """The localization systematic model for the human-in-the loop localization:
    A mixture of a 4.17 deg Gaussian (91.8% weight) and a 15.3 deg Gaussian
    for a centroid between azimuth 292.5 - 67.5 or azimuth 112.5 - 247.5, 
    otherwise a mixture of a 2.31 deg Gaussian (88.4% weight) and a 
    13.2 deg Gaussian.
    
    References:
        arXiv:1411.2685
    
    Args:
        az (float): The localization centroid in spacecraft azimuth
    """
    if (az > 292.5) or (az <= 67.5) or ((az > 112.5) and (az < 247.5)):
        sigma1 = np.deg2rad(4.17)
        sigma2 = np.deg2rad(15.3)
        frac1 = 0.918
    else:
        sigma1 = np.deg2rad(2.31)
        sigma2 = np.deg2rad(13.2)
        frac1 = 0.884
    return ([sigma1, sigma2], [frac1])


def GA_Model():
    """The localization systematic model for the Ground-Automated localization:
    A mixture of a 3.72 deg Gaussian (80.4% weight) and a 13.7 deg Gaussian.
    
    References:
        arXiv:1411.2685
    """
    sigma1 = np.deg2rad(3.72)
    sigma2 = np.deg2rad(13.7)
    frac1 = 0.804
    return ([sigma1, sigma2], [frac1])


def RoboBA_Function(grb_type):
    """The localization systematic model for the RoboBA localization:
    A mixture of a 1.86 deg Gaussian (57.9% weight) and a 4.14 deg Gaussian
    for a "long" GRB, and a mixture of a 2.55 deg Gaussian (39.0% weight) and a 
    4.43 deg Gaussian for a "short" GRB.
    
    References:
        arXiv:1909.03006)
    
    Args:
        grb_type (str): The type of GRB, either 'long' or 'short'
    """
    if grb_type == 'long':
        sigma1 = np.deg2rad(1.86)
        sigma2 = np.deg2rad(4.14)
        frac1 = 0.579
    elif grb_type == 'short':
        sigma1 = np.deg2rad(2.55)
        sigma2 = np.deg2rad(4.43)
        frac1 = 0.39
    else:
        raise ValueError("grb_type must either be 'long' or 'short'")
    return ([sigma1, sigma2], [frac1])


def Untargeted_Search_Model():
    """The localization systematic model for the Untargeted Search:
    A 5.53 deg Gaussian
    """
    sigma = np.deg2rad(5.53)
    return ([sigma], [1.0])
