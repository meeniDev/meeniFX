#version 330
uniform sampler2D source;
out vec4 result;
void main() {
    vec4 pixel = texelFetch(source, ivec2(gl_FragCoord.xy), 0);
    float alpha = clamp(pixel.a, 0., 1.);
    vec3 linear = alpha > 1e-8 ? clamp(pixel.rgb / alpha, 0., 1.) : vec3(0);
    vec3 srgb = mix(1.055 * pow(linear, vec3(1./2.4)) - .055,
                    linear * 12.92, lessThanEqual(linear, vec3(.0031308)));
    result = vec4(srgb.bgr, alpha);
}
