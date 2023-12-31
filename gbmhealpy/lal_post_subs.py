# Copyright (C) 2012-2016  Leo Singer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import warnings
import numpy as np
from astropy.utils.exceptions import AstropyDeprecationWarning

warnings.filterwarnings("ignore", category=AstropyDeprecationWarning)
"""
LalInference post-processing plotting subroutines
"""
import astropy.coordinates
import astropy.units as u

try:
    from astropy.coordinates.angles import rotation_matrix
except:
    from astropy.coordinates.matrix_utilities import rotation_matrix

def find_greedy_credible_levels(p, ranking=None):
    p = np.asarray(p)
    pflat = p.ravel()
    if ranking is None:
        ranking = pflat
    else:
        ranking = np.ravel(ranking)
    i = np.flipud(np.argsort(ranking))
    cs = np.cumsum(pflat[i])
    cls = np.empty_like(pflat)
    cls[i] = cs
    return cls.reshape(p.shape)


def reference_angle(a):
    """Convert an angle to a reference angle between -pi and pi."""
    a = np.mod(a, 2 * np.pi)
    return np.where(a <= np.pi, a, a - 2 * np.pi)


def wrapped_angle(a):
    """Convert an angle to a reference angle between 0 and 2*pi."""
    return np.mod(a, 2 * np.pi)


def make_circle_poly(radius, theta, phi, n=12, closed=False):
    """RA and Dec of polygonized cone about celestial pole"""
    ra_v = 2 * np.pi * np.arange(n) / n
    dec_v = np.ones_like(ra_v) * (0.5 * np.pi - radius)
    M1 = rotation_matrix(phi, 'z', unit=astropy.units.radian)
    M2 = rotation_matrix(theta, 'y', unit=astropy.units.radian)
    R = np.asarray(np.dot(M2, M1))
    xyz = np.dot(R.T,
                 astropy.coordinates.spherical_to_cartesian(1, dec_v, ra_v))
    _, dec_v, ra_v = astropy.coordinates.cartesian_to_spherical(*xyz)
    ra_v = ra_v.to(u.rad).value
    dec_v = dec_v.to(u.rad).value
    ra_v = np.mod(ra_v, 2 * np.pi)
    if closed:
        ra_v = np.concatenate((ra_v, [ra_v[0]]))
        dec_v = np.concatenate((dec_v, [dec_v[0]]))
    return np.transpose((ra_v, dec_v))


try:
    from mpl_toolkits.basemap import _geoslib as geos

    def cut_prime_meridian(vertices):
        """Cut a polygon across the prime meridian, possibly splitting it into multiple
        polygons.  Vertices consist of (longitude, latitude) pairs where longitude
        is always given in terms of a wrapped angle (between 0 and 2*pi).

        This routine is not meant to cover all possible cases; it will only work for
        convex polygons that extend over less than a hemisphere."""

        out_vertices = []

        # Ensure that the list of vertices does not contain a repeated endpoint.
        if (vertices[0, :] == vertices[-1, :]).all():
            vertices = vertices[:-1, :]

        # Ensure that the longitudes are wrapped from 0 to 2*pi.
        vertices = np.column_stack((wrapped_angle(vertices[:, 0]), vertices[:, 1]))

        def count_meridian_crossings(phis):
            n = 0
            for i in range(len(phis)):
                if crosses_meridian(phis[i - 1], phis[i]):
                    n += 1
            return n

        def crosses_meridian(phi0, phi1):
            """Test if the segment consisting of v0 and v1 croses the meridian."""
            # If the two angles are in [0, 2pi), then the shortest arc connecting
            # them crosses the meridian if the difference of the angles is greater
            # than pi.
            phi0, phi1 = sorted((phi0, phi1))
            return phi1 - phi0 > np.pi

        # Count the number of times that the polygon crosses the meridian.
        meridian_crossings = count_meridian_crossings(vertices[:, 0])

        if meridian_crossings % 2:
            # FIXME: Use this simple heuristic to decide which pole to enclose.
            sign_lat = np.sign(np.sum(vertices[:, 1]))

            # If there are an odd number of meridian crossings, then the polygon
            # encloses the pole. Any meridian-crossing edge has to be extended
            # into a curve following the nearest polar edge of the map.
            for i in range(len(vertices)):
                v0 = vertices[i - 1, :]
                v1 = vertices[i, :]
                # Loop through the edges until we find one that crosses the meridian.
                if crosses_meridian(v0[0], v1[0]):
                    # If this segment crosses the meridian, then fill it to
                    # the edge of the map by inserting new line segments.

                    # Find the latitude at which the meridian crossing occurs by
                    # linear interpolation.
                    delta_lon = abs(reference_angle(v1[0] - v0[0]))
                    lat = abs(reference_angle(v0[0])) / delta_lon * v0[1] + abs(
                        reference_angle(v1[0])) / delta_lon * v1[1]

                    # Find the closer of the left or the right map boundary for
                    # each vertex in the line segment.
                    lon_0 = 0. if v0[0] < np.pi else 2 * np.pi
                    lon_1 = 0. if v1[0] < np.pi else 2 * np.pi

                    # Set the output vertices to the polar cap plus the original
                    # vertices.
                    out_vertices += [np.vstack((vertices[:i, :], [
                        [lon_0, lat],
                        [lon_0, sign_lat * np.pi / 2],
                        [lon_1, sign_lat * np.pi / 2],
                        [lon_1, lat],
                    ], vertices[i:, :]))]

                    # Since the polygon is assumed to be convex, the only possible
                    # odd number of meridian crossings is 1, so we are now done.
                    break
        elif meridian_crossings:
            # Since the polygon is assumed to be convex, if there is an even number
            # of meridian crossings, we know that the polygon does not enclose
            # either pole. Then we can use ordinary Euclidean polygon intersection
            # algorithms.

            # Construct polygon representing map boundaries in longitude and latitude.
            frame_poly = geos.Polygon(np.asarray(
                [[0., np.pi / 2], [0., -np.pi / 2], [2 * np.pi, -np.pi / 2],
                 [2 * np.pi, np.pi / 2]]))

            # Intersect with polygon re-wrapped to lie in [pi, 3*pi).
            poly = geos.Polygon(np.column_stack(
                (reference_angle(vertices[:, 0]) + 2 * np.pi, vertices[:, 1])))
            if poly.intersects(frame_poly):
                out_vertices += [p.get_coords() for p in
                                 poly.intersection(frame_poly)]

            # Intersect with polygon re-wrapped to lie in [-pi, pi).
            poly = geos.Polygon(
                np.column_stack((reference_angle(vertices[:, 0]), vertices[:, 1])))
            if poly.intersects(frame_poly):
                out_vertices += [p.get_coords() for p in
                                 poly.intersection(frame_poly)]
        else:
            # Otherwise, there were zero meridian crossings, so we can use the
            # original vertices as is.
            out_vertices += [vertices]

        # Done!
        return out_vertices

except:
    warnings.warn('Basemap not installed. Some functionality not available.')


# -----------------------------------------------------------------------------
